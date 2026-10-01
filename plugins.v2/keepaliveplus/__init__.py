# -*- coding: utf-8 -*-
"""
保号状态增强插件（KeepAlivePlus）。

基于 MoviePilot 站点快照与规则库，切实显示全部站点的保号状态：
- 豁免标记识别：简繁体 + 措辞变体全收（永远/永遠/永久/封存/不活跃/降级豁免等）
- 等级匹配增强：精确 / 双向包含 / 括号剥离 / nameAka
- 无规则站点显示「登录保号中（每日自动登录覆盖）」而非「无法判断」
- 捐赠/黄星站点可在配置中标记，直接显示已保号

规则库复用 PTDepilerMp 的 site_rules 目录（软依赖，缺失时按无规则处理）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class KeepAlivePlus(_PluginBase):
    """基于站点快照与规则库，切实显示全部站点保号状态。"""

    plugin_name = "保号状态增强"
    plugin_desc = "基于站点快照与规则库，切实显示全部站点保号状态（无无法判断）。"
    plugin_icon = "database.png"
    plugin_version = "0.1.3"
    plugin_author = "leon"
    author_url = ""
    plugin_config_prefix = "keepaliveplus_"
    plugin_order = 21
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _cron = ""
    _donor_sites: List[str] = []
    _scheduler: Optional[BackgroundScheduler] = None

    # 规则库候选路径（软依赖：存在即用）
    RULES_DIRS = [
        "/app/app/plugins/ptdepilermp/site_rules",
    ]
    # 保号豁免关键词（简繁 + 措辞变体全收）
    RETAIN_KEYWORDS = (
        "永远保留", "永遠保留", "永久保留", "账号永久保留", "賬號永久保留",
        "不会被删除", "不會被刪除",
        "不会因不活跃", "不會因不活躍",
        "免除自动降级", "免除自動降級",
        "长期不活动不访问", "長期不活動不訪問",
    )
    SUB_PATTERNS = [".zh.srt"]
    VIDEO_EXTS = [".mkv", ".mp4", ".ts", ".avi", ".m2ts"]

    def __init__(self):
        super().__init__()
        self._cached_rows: List[Dict[str, Any]] = []
        self._has_calculated = False

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = str(config.get("cron") or "").strip()
        donor = config.get("donor_sites") or []
        self._donor_sites = [str(s).strip() for s in (donor if isinstance(donor, list) else str(donor).split(",")) if str(s).strip()]
        if self._enabled or self._onlyonce:
            self._recalculate("插件加载")
        if self._onlyonce:
            self._onlyonce = False
            self.__save_config()
        self._setup_scheduler()

    def __save_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "donor_sites": ",".join(self._donor_sites) if isinstance(self._donor_sites, list) else str(self._donor_sites or ""),
        })

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        """无额外 API 端点。"""
        return []

    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """拼装插件配置页面（Vuetify 组件）。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时重算周期（cron 表达式）",
                                            "placeholder": "0 7 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "donor_sites",
                                            "label": "捐赠/黄星站点标识（逗号或换行分隔）",
                                            "rows": 3,
                                            "placeholder": "hdsky,pterclub",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "0 7 * * *",
            "donor_sites": "",
        }

    def _setup_scheduler(self):
        self.stop_service()
        if self._cron and self._enabled:
            try:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self._recalculate, trigger=CronTrigger.from_crontab(self._cron),
                    name="保号状态增强重算", id="keepaliveplus_recalc",
                )
                self._scheduler.start()
                logger.info("%s 定时重算任务已注册：cron=%s", self.plugin_name, self._cron)
            except Exception as err:
                logger.error("%s cron 无效：%s", self.plugin_name, err)

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_job("keepaliveplus_recalc")
        except Exception:
            pass

    # ---------------- 数据与判定 ----------------

    @property
    def rules_dir(self) -> Optional[Path]:
        for d in self.RULES_DIRS:
            if os.path.isdir(d):
                return Path(d)
        return None

    @staticmethod
    def _video_duration(video: str) -> Optional[float]:
        return None  # 本插件不做对齐，时长判定由字幕审查脚本承担

    def _snapshot_rows(self) -> List[Dict[str, Any]]:
        try:
            con = sqlite3.connect("file:/config/user.db?mode=ro", uri=True, timeout=10)
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                select domain, name, user_level, ratio, bonus, upload, download, updated_time
                from siteuserdata
                where id in (select max(id) from siteuserdata group by domain)
                order by updated_time desc
            """).fetchall()
            con.close()
            return [dict(r) for r in rows]
        except Exception as err:
            logger.error("%s 读取站点快照失败：%s", self.plugin_name, err)
            return []

    def _load_rule(self, site_name: str) -> Optional[Dict[str, Any]]:
        rd = self.rules_dir
        if not rd:
            return None
        exact = rd / ("%s.json" % site_name)
        if exact.exists():
            try:
                return json.loads(exact.read_text(encoding="utf-8"))
            except Exception:
                return None
        low = site_name.lower()
        for fn in os.listdir(rd):
            if fn.lower().startswith(low[:4]) and fn.endswith(".json"):
                try:
                    return json.loads((rd / fn).read_text(encoding="utf-8"))
                except Exception:
                    return None
        return None

    @staticmethod
    def _is_retain_priv(priv: str) -> bool:
        return any(kw in priv for kw in KeepAlivePlus.RETAIN_KEYWORDS)

    def _retention_level(self, levels: List[Dict[str, Any]]):
        best = None
        for lv in levels:
            priv = lv.get("privilege") or ""
            if self._is_retain_priv(priv):
                if best is None or int(lv.get("id", 0) or 0) < int(best.get("id", 0) or 0):
                    best = lv
        return best

    @staticmethod
    def _match_level(levels: List[Dict[str, Any]], user_level: str):
        if not user_level:
            return None
        ul = str(user_level)
        ul_clean = re.sub(r"[(（].*?[)）]", "", ul).strip()
        for lv in levels:
            n = str(lv.get("name") or "").strip()
            if n and n.lower() == ul_clean.lower():
                return lv
        for lv in levels:
            n = str(lv.get("name") or "").strip()
            if n and (n in ul or ul in n):
                return lv
            for aka in lv.get("nameAka", []) or []:
                aka = str(aka)
                if aka and (aka in ul or ul in aka):
                    return lv
        for lv in levels:
            n = str(lv.get("name") or "").strip()
            if n and (n in ul_clean or ul_clean in n):
                return lv
        return None

    def _is_donor(self, site_name: str) -> bool:
        low = site_name.lower()
        return any(d.lower() in low or low in d.lower() for d in self._donor_sites if d)

    def _recalculate(self, trigger: str = "内部调用"):
        rows = self._snapshot_rows()
        out = []
        for r in rows:
            domain = r.get("domain") or ""
            site_name = r.get("name") or domain
            user_level = r.get("user_level")
            rule = self._load_rule(site_name)
            levels = (rule or {}).get("levels", [])
            ret_lv = self._retention_level(levels) if levels else None
            cur_lv = self._match_level(levels, user_level) if levels else None
            donor = self._is_donor(site_name)
            if donor:
                status, note = "✅ 已保号", "捐赠/黄星站点（配置指定）"
            elif not rule:
                status, note = "ℹ️ 登录保号中", "无站点规则（每日自动登录已覆盖）"
            elif ret_lv is None:
                status, note = "ℹ️ 登录保号中", "规则未定义保号豁免等级（每日自动登录已覆盖）"
            else:
                ret_id = int(ret_lv.get("id", 0) or 0)
                cur_id = int(cur_lv.get("id", 0) or 0) if cur_lv else None
                ret_name = ret_lv.get("name") or "未配置"
                if cur_id is None:
                    status, note = "ℹ️ 登录保号中", "快照等级无法匹配规则等级表（每日自动登录已覆盖）"
                elif cur_id >= ret_id:
                    status, note = "✅ 已保号", "当前等级已达豁免等级「%s」" % ret_name
                else:
                    req = " / ".join(filter(None, [
                        "分享率 " + str(ret_lv.get("ratio")) if ret_lv.get("ratio") else "",
                        "下载 " + str(ret_lv.get("downloaded")) if ret_lv.get("downloaded") else "",
                    ]))
                    status, note = "⚠️ 未保号（登录保号中）", "距豁免等级「%s」还差 %d 级%s" % (
                        ret_name, ret_id - cur_id, ("（需 %s）" % req) if req else "")
            out.append({
                "site": site_name, "domain": domain,
                "user_level": user_level or "无快照",
                "retention_level": (ret_lv or {}).get("name") or "未配置",
                "status": status, "note": note,
                "updated": r.get("updated_time"),
                "donor": donor,
            })
        self._cached_rows = out
        self._has_calculated = True
        logger.info("%s 保号状态重算完成（触发=%s，站点数=%d）", self.plugin_name, trigger, len(out))

    def _rows(self) -> List[Dict[str, Any]]:
        if not self._has_calculated:
            self._recalculate("页面首次计算")
        return self._cached_rows

    @eventmanager.register(EventType.SiteRefreshed)
    def on_all_sites_refreshed(self, event: Event):
        if (event.event_data or {}).get("site_id") != "*":
            return
        try:
            self._recalculate("站点全量刷新通知")
        except Exception as err:
            logger.error("%s 全站刷新后重算失败：%s", self.plugin_name, err)

    # ---------------- 页面 ----------------

    def get_page(self) -> List[dict]:
        """返回保号状态详情页（全站表格）。"""
        rows = sorted(self._rows(), key=lambda r: r.get("site") or "")
        headers = ["站点", "当前等级", "保号状态", "保号豁免等级", "说明", "数据时间"]
        total = len(rows)
        ok_cnt = sum(1 for r in rows if "已保号" in (r.get("status") or ""))
        login_cnt = total - ok_cnt
        summary_text = f"共 {total} 站：\u2705 已保号 {ok_cnt} 站 | \u2139 登录保号 {login_cnt} 站"
        thead = {
            "component": "thead",
            "content": [{
                "component": "tr",
                "content": [{"component": "th", "props": {"class": "text-start"}, "text": h} for h in headers],
            }],
        }
        trs = []
        for r in rows:
            trs.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-sm"}, "text": str(r.get(k) or "")}
                    for k in ("site", "user_level", "status", "retention_level", "note", "updated")
                ],
            })
        vtable = {
            "component": "VTable",
            "props": {"class": "w-100"},
            "content": [thead, {"component": "tbody", "content": trs}],
        }
        return [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {"type": "success", "variant": "tonal"}, "text": summary_text},
                ]},
                {"component": "VCol", "props": {"cols": 12}, "content": [vtable]},
            ]},
        ]

    def get_page_data(self):
        pass
