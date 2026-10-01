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
    plugin_version = "0.3.0"
    plugin_author = "leon"
    author_url = ""
    plugin_config_prefix = "keepaliveplus_"
    plugin_order = 21
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _daily = True
    _monthly = True
    _search_boost = False
    _cron = ""
    _donor_sites: List[str] = []
    _scheduler: Optional[BackgroundScheduler] = None

    # 规则库候选路径（自有目录优先：站点实锤修正版防市场插件更新覆盖；ptdepilermp 目录兜底）
    RULES_DIRS = [
        "/config/keepaliveplus_rules",
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
    # 无条件永久保留关键词（不含封存前提才算真保号）
    PERMANENT_KW = ["永远保留", "永遠保留", "永久保留", "永久不删除", "永久不刪除", "不会因不活跃", "不會因不活躍", "永远不会被删号", "永遠不會被刪號"]
    # 封存/挂起类前提词
    SEAL_KW = ["封存", "挂起", "掛起", "Park", "park"]

    def __init__(self):
        super().__init__()
        self._cached_rows: List[Dict[str, Any]] = []
        self._has_calculated = False

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._daily = bool(config.get("daily_refresh", True))
        self._monthly = bool(config.get("monthly_refresh", True))
        self._search_boost = bool(config.get("search_boost", False))
        self._cron = str(config.get("cron") or "").strip()
        donor = config.get("donor_sites") or []
        self._donor_sites = [str(s).strip() for s in (donor if isinstance(donor, list) else str(donor).split(",")) if str(s).strip()]
        if self._enabled or self._onlyonce:
            self._recalculate("插件加载")
        if self._onlyonce:
            self._onlyonce = False
            self.__save_config()
        self._setup_scheduler()
        # 搜索优选:开启时按下载缺口重排站点优先级,关闭时还原原优先级
        if self._search_boost:
            self._apply_search_priority()
            self._install_seeders_order_patch()
            self._install_stream_sort_patch()
        else:
            self._restore_search_priority()
            self._uninstall_seeders_order_patch()
            self._uninstall_stream_sort_patch()

    def __save_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "daily_refresh": self._daily,
            "monthly_refresh": self._monthly,
            "search_boost": self._search_boost,
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "search_boost",
                                            "label": "搜索优选(按下载缺口重排站点优先级)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "daily_refresh",
                                            "label": "每日刷新2次（07:30/19:30，异常推送提醒）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "monthly_refresh",
                                            "label": "每月1日存档站点规则页并提醒核对",
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
                                            "label": "额外重算周期（cron 表达式，可空）",
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
            "daily_refresh": True,
            "monthly_refresh": True,
            "cron": "",
            "donor_sites": "",
        }

    def _setup_scheduler(self):
        self.stop_service()
        if not (self._enabled and (self._cron or self._daily or self._monthly)):
            return
        try:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        except Exception as err:
            logger.error("%s 调度器创建失败：%s", self.plugin_name, err)
            self._scheduler = None
            return
        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self._recalculate, trigger=CronTrigger.from_crontab(self._cron),
                    name="保号状态增强重算", id="keepaliveplus_recalc",
                )
                logger.info("%s 定时重算任务已注册：cron=%s", self.plugin_name, self._cron)
            except Exception as err:
                logger.error("%s cron 无效：%s", self.plugin_name, err)
        if self._daily:
            try:
                self._scheduler.add_job(
                    func=self._recalculate, trigger=CronTrigger.from_crontab("30 7,19 * * *"),
                    args=["每日定时刷新"], name="每日数据刷新(2次/天)", id="keepaliveplus_daily",
                )
                logger.info("%s 每日数据刷新任务已注册：每天 07:30 / 19:30", self.plugin_name)
            except Exception as err:
                logger.error("%s 每日任务注册失败：%s", self.plugin_name, err)
        if self._monthly:
            try:
                self._scheduler.add_job(
                    func=self._monthly_rules_snapshot, trigger=CronTrigger.from_crontab("0 6 1 * *"),
                    name="月度站点规则存档", id="keepaliveplus_monthly",
                )
                logger.info("%s 月度站点规则存档任务已注册：每月1日 06:00", self.plugin_name)
            except Exception as err:
                logger.error("%s 月度任务注册失败：%s", self.plugin_name, err)
        try:
            self._scheduler.start()
        except Exception as err:
            logger.error("%s 调度器启动失败：%s", self.plugin_name, err)

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_job("keepaliveplus_recalc")
                self._scheduler.remove_job("keepaliveplus_daily")
                self._scheduler.remove_job("keepaliveplus_monthly")
        except Exception:
            pass

    # ---------------- 数据与判定 ----------------

    @property
    def rule_dirs(self) -> List[Path]:
        """所有存在的规则目录（自有目录优先）。"""
        return [Path(d) for d in self.RULES_DIRS if os.path.isdir(d)]

    @staticmethod
    def _video_duration(video: str) -> Optional[float]:
        return None  # 本插件不做对齐，时长判定由字幕审查脚本承担

    def _snapshot_rows(self) -> List[Dict[str, Any]]:
        try:
            con = sqlite3.connect("file:/config/user.db?mode=ro", uri=True, timeout=10)
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                select domain, name, user_level, ratio, bonus, upload, download,
                       join_at, updated_day, err_msg, updated_time
                from siteuserdata
                where id in (select max(id) from siteuserdata group by domain)
                order by updated_time desc
            """).fetchall()
            # 仅保留当前站点表里存在的域名（过滤旧域名残留快照，如憨憨 hhanclub.top）。
            # site.url 可能带 www/子域前缀而快照 domain 是规范域名，故按尾部匹配。
            def _norm(d: str) -> str:
                d = (d or "").lower().strip()
                return d[4:] if d.startswith("www.") else d

            site_domains = set()
            for s in con.execute("select url from site"):
                u = _norm(s["url"] or "")
                if "//" in u:
                    u = u.split("//", 1)[1]
                u = u.split("/", 1)[0].strip()
                if u and "." in u:
                    site_domains.add(u)
            con.close()

            def _matched(dom: str) -> bool:
                d = _norm(dom)
                if not d:
                    return False
                for sd in site_domains:
                    if d == sd or d.endswith("." + sd) or sd.endswith("." + d):
                        return True
                return False

            # sqlite3.Row 没有 .get，先转 dict 再过滤
            items = [dict(r) for r in rows]
            return [r for r in items if _matched(r.get("domain") or "")]
        except Exception as err:
            logger.error("%s 读取站点快照失败：%s", self.plugin_name, err)
            return []

    def _load_rule(self, site_name: str) -> Optional[Dict[str, Any]]:
        dirs = self.rule_dirs
        if not dirs:
            return None
        low = site_name.lower()
        # 先全目录精确匹配（自有目录优先），再退回前缀匹配
        for rd in dirs:
            exact = rd / ("%s.json" % site_name)
            if exact.exists():
                try:
                    return json.loads(exact.read_text(encoding="utf-8"))
                except Exception:
                    continue
        for rd in dirs:
            for fn in os.listdir(rd):
                if fn.lower().startswith(low[:4]) and fn.endswith(".json"):
                    try:
                        return json.loads((rd / fn).read_text(encoding="utf-8"))
                    except Exception:
                        continue
        return None

    def _permanent_level(self, levels: List[Dict[str, Any]]):
        """无条件永久保留档：privilege 含永久/永远保留关键词且不含封存/挂起前提。"""
        best = None
        for lv in levels:
            priv = lv.get("privilege") or ""
            low = priv.lower()
            if any(kw in priv for kw in self.PERMANENT_KW) and not any(kw in low for kw in self.SEAL_KW):
                if best is None or int(lv.get("id", 0) or 0) < int(best.get("id", 0) or 0):
                    best = lv
        return best

    def _seal_level(self, levels: List[Dict[str, Any]]):
        """封存型保号档：达到该等级后封存账号可豁免（不计入无条件保号）。"""
        best = None
        for lv in levels:
            priv = lv.get("privilege") or ""
            low = priv.lower()
            if any(kw in priv for kw in self.RETAIN_KEYWORDS) and any(kw in low for kw in self.SEAL_KW):
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

    @staticmethod
    def _size_gb(s) -> Optional[float]:
        """解析 '512G'/'120GB'/'1T'/'1.5TB'/纯数字(字节或GB) 为 GB 数。"""
        if s is None:
            return None
        if isinstance(s, (int, float)):
            v = float(s)
            # 大于 2^30 视为字节数，否则视为 GB 数
            return v / (1 << 30) if v > (1 << 30) else v
        m = re.match(r"([\d.]+)\s*([TGMK]?)(?:I?B)?$", str(s).strip().upper())
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        factor = {"T": 1024.0, "G": 1.0, "M": 1 / 1024.0, "K": 1 / (1024 * 1024), "": 1.0}.get(m.group(2))
        return v * factor if factor else None

    def _ratio_text(self, lv: Optional[Dict[str, Any]], row: Dict[str, Any]) -> str:
        """分享率表述（列内精简版）：达标✓/未达标✗/无要求。"""
        try:
            cur_v = float(row.get("ratio"))
        except (TypeError, ValueError):
            return "无数据"
        need = lv.get("ratio") if lv else None
        try:
            need_v = float(need) if need is not None else None
        except (TypeError, ValueError):
            need_v = None
        if need_v is None:
            return "%.2f(无要求)" % cur_v
        if cur_v >= need_v:
            return "%.2f✓(需%.2f)" % (cur_v, need_v)
        return "%.2f✗(需%.2f,差%.2f)" % (cur_v, need_v, need_v - cur_v)

    def _field_gap(self, key: str, need, row: Dict[str, Any]) -> Optional[str]:
        """单个条件字段的差距描述；已达标或无法比较返回 None。"""
        if need is None:
            return None
        try:
            if key == "ratio":
                diff = float(need) - float(row.get("ratio") or 0)
                return ("分享率还差%.2f" % diff) if diff > 0 else None
            if key in ("downloaded", "uploaded"):
                need_gb = self._size_gb(need)
                if need_gb is None:
                    return None
                # 规则字段名 downloaded/uploaded 对应快照字段名 download/upload
                rowkey = "download" if key == "downloaded" else "upload"
                label = "下载" if key == "downloaded" else "上传"
                diff = need_gb - (row.get(rowkey) or 0) / (1 << 30)
                if diff <= 0.5:
                    return None
                if diff >= 1024:
                    return "%s还差%.1fTB" % (label, diff / 1024)
                return "%s还差%.0fGB" % (label, diff)
            if key == "bonus":
                diff = float(need) - float(row.get("bonus") or 0)
                return ("魔力还差%.0f" % diff) if diff > 0 else None
        except (TypeError, ValueError):
            return None
        # 快照无对应数据的字段，只能列门槛
        if key == "uploads":
            return "发种%d个(快照无发种数据)" % int(need)
        if key == "snatches":
            return "完成%d个(快照无数据)" % int(need)
        if key == "seedingBonus":
            return "做种积分≥%s(快照无数据)" % need
        if key == "seedingTime":
            return "做种%s(快照无数据)" % need
        return None

    def _gap_text(self, lv: Dict[str, Any], row: Dict[str, Any]) -> str:
        """按快照数据计算到豁免等级的“实际差距”，支持 alternative(二选一)。"""
        alts = lv.get("alternative") or []
        if alts:
            parts = []
            for alt in alts:
                sub = [g for g in (self._field_gap(k, v, row) for k, v in alt.items()) if g]
                parts.append("、".join(sub) if sub else "已达标")
            if any(p == "已达标" for p in parts):
                return ""  # 二选一里已有满足的路径
            return "满足其一:" + " 或 ".join(parts)
        # 分享率单独成段（_ratio_text 已含），这里不再重复
        gaps = [g for g in (self._field_gap(k, lv.get(k), row) for k in ("downloaded", "uploaded", "bonus")) if g]
        m = re.match(r"P(\d+)([WDMY])", str(lv.get("interval") or ""))
        join_at = str(row.get("join_at") or "")
        if m and join_at:
            try:
                joined = datetime.strptime(join_at[:10], "%Y-%m-%d")
                need_days = {"W": 7, "D": 1, "M": 30, "Y": 365}.get(m.group(2), 0) * int(m.group(1))
                alive_days = (datetime.now() - joined).days
                if alive_days < need_days:
                    gaps.append("注册还差%d天" % (need_days - alive_days))
            except ValueError:
                pass
        return "、".join(gaps)

    def _risk_text(self, rule: Optional[Dict[str, Any]], row: Dict[str, Any]) -> str:
        """红线简报（列内精简版）：动作+两档天数+登录链路状态。"""
        if not rule:
            return "—"
        lag = 0
        upd_day = str(row.get("updated_day") or "")
        if upd_day:
            try:
                lag = (datetime.now() - datetime.strptime(upd_day, "%Y-%m-%d")).days
            except ValueError:
                lag = 0
        action = rule.get("risk_action") or ""
        days1 = rule.get("risk_days")
        days2 = rule.get("risk2_days")

        def _r(days):
            if not isinstance(days, (int, float)) or days <= 0:
                return None
            return int(days) - max(lag, 0)

        r1, r2 = _r(days1), _r(days2)
        if r1 is None or not action:
            extra = rule.get("risk_extra") or rule.get("risk_note") or ""
            return extra[:70] if extra else "—"
        sep = "、"
        if lag > 0:
            head = "‼️停更%d天" % lag
            seg = "剩约%d天(未封)" % r1 if r1 > 0 else "已到线"
            if r2 is not None:
                seg += "、剩约%d天(封)" % r2 if r2 > 0 else "、封存档也到线"
        else:
            head = "✓登录覆盖中"
            seg = "约%d天(未封)" % r1
            if r2 is not None:
                seg += "、约%d天(封)" % r2
        return "%s红线%s:%s" % (action, head, sep.join([seg]))

    @staticmethod
    def _cond_text(lv: Dict[str, Any]) -> str:
        """豁免等级达标条件清单（按规则文件字段拼接）。"""
        parts = []
        m = re.match(r"P(\d+)([WDMY])", str(lv.get("interval") or ""))
        if m:
            unit = {"W": "周", "D": "天", "M": "个月", "Y": "年"}.get(m.group(2), m.group(2))
            parts.append("注册≥%s%s" % (m.group(1), unit))
        if lv.get("downloaded"):
            parts.append("下载≥%s" % lv["downloaded"])
        if lv.get("uploaded"):
            parts.append("上传≥%s" % lv["uploaded"])
        if lv.get("ratio"):
            parts.append("分享率≥%s" % lv["ratio"])
        if lv.get("bonus"):
            parts.append("魔力≥%s" % lv["bonus"])
        m2 = re.match(r"P(\d+)D", str(lv.get("seedingTime") or ""))
        if m2:
            parts.append("做种≥%s天" % m2.group(1))
        if lv.get("uploads"):
            parts.append("发种≥%s个" % lv["uploads"])
        if lv.get("snatches"):
            parts.append("完成≥%s个" % lv["snatches"])
        alts = lv.get("alternative") or []
        if alts:
            alt_strs = []
            for alt in alts:
                seg = []
                for k, v in alt.items():
                    if k == "downloaded":
                        seg.append("下载≥%s" % v)
                    elif k == "uploaded":
                        seg.append("上传≥%s" % v)
                    elif k == "uploads":
                        seg.append("发种≥%s个" % v)
                    elif k == "ratio":
                        seg.append("分享率≥%s" % v)
                    elif k == "seedingBonus":
                        seg.append("做种积分≥%s" % v)
                    elif k == "snatches":
                        seg.append("完成≥%s个" % v)
                    else:
                        seg.append("%s≥%s" % (k, v))
                alt_strs.append("+".join(seg))
            parts.append("满足其一:" + " 或 ".join(alt_strs))
        return "、".join(parts)

    def _recalculate(self, trigger: str = "内部调用"):
        try:
            rows = self._snapshot_rows()
        except Exception as err:
            logger.error("%s 读取站点快照异常：%s", self.plugin_name, err)
            try:
                self.post_message(title="%s 数据刷新异常" % self.plugin_name,
                                  text="读取站点快照失败：%s" % err)
            except Exception:
                pass
            return
        if not rows:
            logger.warning("%s 站点快照为空，可能站点数据未刷新", self.plugin_name)
            try:
                self.post_message(title="%s 数据刷新异常" % self.plugin_name,
                                  text="站点快照为空：请检查 PTDepilerMp 站点刷新是否正常运行。")
            except Exception:
                pass
        out = []
        for r in rows:
            domain = r.get("domain") or ""
            site_name = r.get("name") or domain
            user_level = r.get("user_level")
            rule = self._load_rule(site_name)
            levels = (rule or {}).get("levels", [])
            perm_lv = self._permanent_level(levels) if levels else None
            seal_lv = self._seal_level(levels) if levels else None
            cur_lv = self._match_level(levels, user_level) if levels else None
            donor = self._is_donor(site_name)
            tgt_lv = perm_lv if perm_lv is not None else seal_lv
            ratio_txt = self._ratio_text(tgt_lv, r)
            risk_txt = self._risk_text(rule, r)
            gap_gb = self._download_gap_gb(tgt_lv, r) if tgt_lv is not None else 0.0
            gap_txt = "—"
            if donor:
                status = "✅ 已保号"
                gap_txt = "捐赠/黄星站点"
            elif not rule:
                status = "ℹ️ 登录保号中"
                gap_txt = "无站点规则"
                risk_txt = "—"
            elif perm_lv is None and seal_lv is None:
                status = "ℹ️ 登录保号中"
                gap_txt = "规则无豁免档"
                risk_txt = "—"
            else:
                # 判定基准=无条件永久保留档（封存型豁免不算已保号）
                tgt_id = int(tgt_lv.get("id", 0) or 0)
                tgt_name = tgt_lv.get("name") or "未配置"
                cur_id = int(cur_lv.get("id", 0) or 0) if cur_lv else None
                if cur_id is None:
                    status = "ℹ️ 登录保号中"
                    gap_txt = "等级未匹配"
                elif perm_lv is not None and cur_id >= tgt_id:
                    status = "✅ 已保号"
                    gap_txt = "已达「%s」永久保留" % tgt_name
                    # 无条件永久保留 = 不活跃红线不适用；仅全员冻结/禁用型(春天/北洋园)例外
                    if not rule.get("risk_applies_to_permanent"):
                        risk_txt = "—"
                elif perm_lv is None and cur_id >= tgt_id:
                    status = "✅ 已达标(封存型)"
                    gap_txt = "已达「%s」,封存后豁免" % tgt_name
                else:
                    status = "⚠️ 未保号"
                    gaps = self._gap_text(tgt_lv, r)
                    cond = self._cond_text(tgt_lv)
                    detail = gaps or cond or "见站点规则"
                    gap_txt = "距「%s」差%d级\n%s" % (tgt_name, tgt_id - cur_id, detail)
            out.append({
                "site": site_name, "domain": domain,
                "user_level": user_level or "无快照",
                "status": status, "ratio": ratio_txt, "gap": gap_txt, "risk": risk_txt,
                "gap_gb": round(gap_gb, 1),
                "updated": r.get("updated_time"),
                "err_msg": r.get("err_msg"),
                "donor": donor,
            })
        self._cached_rows = out
        self._has_calculated = True
        logger.info("%s 保号状态重算完成（触发=%s，站点数=%d）", self.plugin_name, trigger, len(out))
        # 搜索优选：按最新缺口重排站点搜索优先级
        if self._search_boost:
            self._apply_search_priority()
        # 刷新异常提示：等级缺失 / 快照报错（err_msg）的站点
        bad = []
        for o in out:
            if not o.get("user_level") or o["user_level"] == "无快照":
                bad.append("%s(无等级)" % o["site"])
            elif o.get("err_msg"):
                bad.append("%s(%s)" % (o["site"], str(o["err_msg"])[:40]))
        if bad:
            try:
                self.post_message(title="%s 数据刷新异常" % self.plugin_name,
                                  text="以下站点快照异常：%s。请检查对应站点 Cookie/登录状态。" % "、".join(bad))
            except Exception:
                pass

    def _download_gap_gb(self, lv: Optional[Dict[str, Any]], row: Dict[str, Any]) -> float:
        """到永久档的下载量缺口(GB)；无下载要求或已达标返回 0。

        alternative(二选一)结构时取"下载路径"的缺口作为参考值。
        """
        if lv is None:
            return 0.0
        need = None
        alts = lv.get("alternative") or []
        for alt in alts:
            if alt.get("downloaded") is not None:
                need = alt.get("downloaded")
                break
        if need is None:
            need = lv.get("downloaded")
        need_gb = self._size_gb(need)
        if need_gb is None:
            return 0.0
        cur = (row.get("download") or 0) / (1 << 30)
        return max(0.0, need_gb - cur)

    def _apply_search_priority(self):
        """搜索优选:两件事。

        1. 按下载缺口升序重排 site.pri(排序引擎里 999-pri 越大=越靠前);
        2. 默认排序引擎(sort_torrents)的规则 TorrentsPriority 改为
           ["site","seeder"]——站点优先级优先 + 做种数降序,
           搜索结果链(result.py)默认即保号优选排序,前端无需任何操作。

        首次启用时备份原 pri 与 TorrentsPriority 到 /config/keepaliveplus_pri_backup.json,
        关闭功能时由 _restore_search_priority 还原。
        """
        if not self._has_calculated:
            self._recalculate("搜索优选前置计算")
        try:
            import json as _json
            backup_file = Path("/config/keepaliveplus_pri_backup.json")
            con = sqlite3.connect("/config/user.db", timeout=15)
            con.row_factory = sqlite3.Row
            # 备份(兼容旧结构:纯 {id:pri})
            backup = {"site_pri": {}, "torrents_priority": '["torrent", "upload", "seeder"]'}
            if backup_file.exists():
                try:
                    old = _json.loads(backup_file.read_text(encoding="utf-8"))
                    if "site_pri" in old:
                        backup["site_pri"] = old.get("site_pri") or {}
                        backup["torrents_priority"] = old.get("torrents_priority") or backup["torrents_priority"]
                    else:
                        backup["site_pri"] = old
                except Exception:
                    pass
            if not backup["site_pri"]:
                backup["site_pri"] = {str(r["id"]): (r["pri"] or 0) for r in con.execute("select id, pri from site")}
            r = con.execute("select value from systemconfig where key='TorrentsPriority'").fetchone()
            backup["torrents_priority"] = (r["value"] if r else None) or backup["torrents_priority"]
            backup_file.write_text(_json.dumps(backup, ensure_ascii=False), encoding="utf-8")
            logger.info("%s 已备份原站点优先级(%d 站)与排序规则", self.plugin_name, len(backup["site_pri"]))

            def _norm(d):
                d = (d or "").lower().strip()
                return d[4:] if d.startswith("www.") else d

            site_map = {}
            for s in con.execute("select id, url from site"):
                u = _norm(s["url"] or "")
                if "//" in u:
                    u = u.split("//", 1)[1]
                u = u.split("/", 1)[0].strip()
                if u:
                    # 同一域名可能有多条站点记录(重复添加),全部收集
                    site_map.setdefault(u, []).append(s["id"])

            def _match_ids(dom):
                d = _norm(dom)
                ids = []
                for u, sids in site_map.items():
                    if d == u or d.endswith("." + u) or u.endswith("." + d):
                        ids.extend(sids)
                return ids

            # 排序:有缺口的站在前(缺口升序,缺得少的最优先);缺口0(已保号)排后
            items = []
            for o in self._cached_rows:
                ids = _match_ids(o.get("domain") or "")
                for sid in ids:
                    items.append((sid, float(o.get("gap_gb") or 0)))
            items.sort(key=lambda x: (x[1] <= 0, x[1], x[0]))
            pri = 0
            for sid, _ in items:
                pri += 1
                con.execute("update site set pri=? where id=?", (pri, sid))
            # 默认排序引擎规则:站点优先级 + 做种数降序
            con.execute("update systemconfig set value=? where key='TorrentsPriority'",
                        ('["site","seeder"]',))
            con.commit()
            con.close()
            logger.info("%s 搜索优选已生效:站点优先级重排 %d 条 + 排序规则[site,seeder]", self.plugin_name, len(items))
        except Exception as err:
            logger.error("%s 搜索优选重排失败:%s", self.plugin_name, err)

    def _restore_search_priority(self):
        """关闭搜索优选时还原备份的原站点优先级与排序规则。"""
        backup_file = Path("/config/keepaliveplus_pri_backup.json")
        if not backup_file.exists():
            return
        try:
            import json as _json
            data = _json.loads(backup_file.read_text(encoding="utf-8"))
            pri_map = data.get("site_pri") or data
            con = sqlite3.connect("/config/user.db", timeout=15)
            for sid, pri in pri_map.items():
                con.execute("update site set pri=? where id=?", (int(pri), int(sid)))
            tp = data.get("torrents_priority")
            if tp:
                con.execute("update systemconfig set value=? where key='TorrentsPriority'", (tp,))
            con.commit()
            con.close()
            backup_file.unlink()
            logger.info("%s 已还原原站点优先级与排序规则(%d 站)", self.plugin_name, len(pri_map))
        except Exception as err:
            logger.error("%s 还原站点优先级失败:%s", self.plugin_name, err)

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

    def _monthly_rules_snapshot(self):
        """月度站点规则存档：重抓各站 FAQ/规则页落盘，完成后推送提醒人工核对。"""
        import time as _time
        from datetime import datetime
        try:
            import requests
        except Exception as err:
            logger.error("%s 月度存档缺少 requests：%s", self.plugin_name, err)
            return
        ym = datetime.now().strftime("%Y%m")
        outdir = Path("/config/_wb_keepalive_raw/monthly") / ym
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as err:
            logger.error("%s 月度存档目录创建失败：%s", self.plugin_name, err)
            return
        con = sqlite3.connect("file:/config/user.db?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        sites = con.execute("select id, name, url, cookie, ua from site order by id").fetchall()
        con.close()
        seen, ok, fail = set(), [], []
        for s in sites:
            base = (s["url"] or "").rstrip("/")
            dom = base.split("//")[-1].split("/")[0]
            if not base or dom in seen:
                continue
            seen.add(dom)
            headers = {
                "User-Agent": (s["ua"] or "").strip() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Cookie": s["cookie"] or "",
                "Referer": base + "/index.php",
            }
            got = []
            for page in ("rules.php", "faq.php", "wiki.php"):
                try:
                    resp = requests.get(base + "/" + page, headers=headers, timeout=25, allow_redirects=True)
                    if resp.status_code == 200 and len(resp.content) > 3000:
                        (outdir / ("%d_%s_%s.html" % (s["id"], dom.split(".")[0], page.replace(".php", "")))).write_bytes(resp.content)
                        got.append(page)
                except Exception:
                    pass
                _time.sleep(3)
            (ok if got else fail).append(s["name"])
        summary = "成功 %d 站：%s%s失败 %d 站：%s%s存档目录：%s%s请打开存档页面对照保号等级规则是否有变化，有变化请告知助手更新规则文件。" % (
            len(ok), "、".join(ok), "\n", len(fail), "、".join(fail), "\n", outdir, "\n")
        try:
            self.post_message(title="%s 月度站点规则存档完成" % self.plugin_name, text=summary)
        except Exception as err:
            logger.error("%s 月度存档通知发送失败：%s", self.plugin_name, err)
        logger.info("%s 月度站点规则存档完成：成功%d/失败%d", self.plugin_name, len(ok), len(fail))

    # ---------------- 页面 ----------------

    def get_page(self) -> List[dict]:
        """返回保号状态详情页（分列排版：状态着色，红线/差距/分享率独立列）。"""
        rows = sorted(self._rows(), key=lambda r: r.get("site") or "")
        headers = ["站点", "当前等级", "保号状态", "分享率", "距永久档差距", "不活跃红线", "数据时间"]
        total = len(rows)
        ok_cnt = sum(1 for r in rows if "已保号" in (r.get("status") or ""))
        seal_cnt = sum(1 for r in rows if "封存型" in (r.get("status") or ""))
        warn_cnt = sum(1 for r in rows if "未保号" in (r.get("status") or ""))
        login_cnt = total - ok_cnt - seal_cnt - warn_cnt
        summary_text = f"共 {total} 站：✅ 已保号 {ok_cnt} 站 | ✅ 已达标(封存型) {seal_cnt} 站 | ⚠️ 未保号 {warn_cnt} 站 | ℹ️ 登录保号中 {login_cnt} 站"
        thead = {
            "component": "thead",
            "content": [{
                "component": "tr",
                "content": [{"component": "th", "props": {"class": "text-start"}, "text": h} for h in headers],
            }],
        }

        def _status_cls(st):
            if "✅" in st:
                return "text-success"
            if "⚠️" in st:
                return "text-warning"
            return "text-info"

        def _ratio_cls(rt):
            if "✓" in rt:
                return "text-success"
            if "✗" in rt:
                return "text-error"
            return "text-start"

        def _risk_cls(rk):
            if "‼️" in rk:
                return "text-error"
            if rk in ("—", ""):
                return "text-start"
            return "text-warning"

        trs = []
        for r in rows:
            st = str(r.get("status") or "")
            rt = str(r.get("ratio") or "")
            rk = str(r.get("risk") or "")
            cells = [
                ("site", "text-start font-weight-medium"),
                ("user_level", "text-start"),
                ("status", "text-start " + _status_cls(st)),
                ("ratio", "text-start " + _ratio_cls(rt)),
                ("gap", "text-start"),
                ("risk", "text-start " + _risk_cls(rk)),
                ("updated", "text-start"),
            ]
            trs.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": cls}, "text": str(r.get(k) or "")}
                    for k, cls in cells
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
