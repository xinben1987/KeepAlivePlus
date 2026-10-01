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
    plugin_version = "0.1.6"
    plugin_author = "leon"
    author_url = ""
    plugin_config_prefix = "keepaliveplus_"
    plugin_order = 21
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _daily = True
    _monthly = True
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
            "daily_refresh": self._daily,
            "monthly_refresh": self._monthly,
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
                                "props": {"cols": 12, "md": 6},
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

    @staticmethod
    def _size_gb(s) -> Optional[float]:
        """解析 '512G'/'120GB'/'1T'/'1.5TB'/'25000G' 为 GB 数。"""
        if not s:
            return None
        m = re.match(r"([\d.]+)\s*([TGMK]?)(?:I?B)?$", str(s).strip().upper())
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        factor = {"T": 1024.0, "G": 1.0, "M": 1 / 1024.0, "K": 1 / (1024 * 1024), "": 1.0}.get(m.group(2))
        return v * factor if factor else None

    def _gap_text(self, lv: Dict[str, Any], row: Dict[str, Any]) -> str:
        """按快照数据计算到豁免等级的“实际差距”，无法计算的项不列。"""
        gaps = []
        if lv.get("ratio") is not None:
            try:
                diff = float(lv["ratio"]) - float(row.get("ratio") or 0)
                if diff > 0:
                    gaps.append("分享率还差%.2f" % diff)
            except (TypeError, ValueError):
                pass
        need_dl = self._size_gb(lv.get("downloaded"))
        if need_dl is not None:
            diff = need_dl - (row.get("download") or 0) / (1 << 30)
            if diff > 0.5:
                gaps.append(("下载还差%.1fTB" % (diff / 1024)) if diff >= 1024 else ("下载还差%.0fGB" % diff))
        need_ul = self._size_gb(lv.get("uploaded"))
        if need_ul is not None:
            diff = need_ul - (row.get("upload") or 0) / (1 << 30)
            if diff > 0.5:
                gaps.append(("上传还差%.1fTB" % (diff / 1024)) if diff >= 1024 else ("上传还差%.0fGB" % diff))
        if lv.get("bonus") is not None:
            try:
                diff = float(lv["bonus"]) - float(row.get("bonus") or 0)
                if diff > 0:
                    gaps.append(("魔力还差%.0f" % diff) if diff >= 1 else "魔力已达标")
            except (TypeError, ValueError):
                pass
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
        """按当前状态生成红线提示：两档（未封存/封存）距离禁用/删除红线还剩多少。"""
        if not rule:
            return ""
        lag = 0
        upd_day = str(row.get("updated_day") or "")
        if upd_day:
            try:
                lag = (datetime.now() - datetime.strptime(upd_day, "%Y-%m-%d")).days
            except ValueError:
                lag = 0

        def _seg(days, action, label) -> str:
            if not isinstance(days, (int, float)) or days <= 0 or not action:
                return ""
            days = int(days)
            dstr = "%d周(%d天)" % (days // 7, days) if days % 7 == 0 else "%d天" % days
            remain = days - max(lag, 0)
            if remain <= 0:
                return "%s连续%s不登录→%s(‼️已到红线)" % (label, dstr, action)
            return "%s连续%s不登录→%s(剩约%d天)" % (label, dstr, action, remain)

        parts = []
        seg1 = _seg(rule.get("risk_days"), rule.get("risk_action"), "未封存")
        if seg1:
            parts.append(seg1)
        seg2 = _seg(rule.get("risk2_days"), rule.get("risk2_action"), "封存")
        if seg2:
            parts.append(seg2)
        if parts:
            txt = "、".join(parts)
            if lag > 0:
                txt += ";‼️快照已停更%d天" % lag
            else:
                txt += ";当前每日登录覆盖中"
            return txt
        extra = rule.get("risk_extra") or rule.get("risk_note") or ""
        return ("预计:%s" % extra) if extra else ""

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
            risk_txt = self._risk_text(rule, r)
            ret_lv = self._retention_level(levels) if levels else None
            cur_lv = self._match_level(levels, user_level) if levels else None
            donor = self._is_donor(site_name)
            if donor:
                status, note = "✅ 已保号", "捐赠/黄星站点（配置指定）"
                if risk_txt:
                    note += "。⚠️ " + risk_txt
            elif not rule:
                status, note = "ℹ️ 登录保号中", "无站点规则（每日自动登录已覆盖）"
            elif ret_lv is None:
                status, note = "ℹ️ 登录保号中", "规则未定义保号豁免等级（每日自动登录已覆盖）"
                if risk_txt:
                    note += "。⚠️ " + risk_txt
            else:
                ret_id = int(ret_lv.get("id", 0) or 0)
                cur_id = int(cur_lv.get("id", 0) or 0) if cur_lv else None
                ret_name = ret_lv.get("name") or "未配置"
            out.append({
                "site": site_name, "domain": domain,
                "user_level": user_level or "无快照",
                "retention_level": (ret_lv or {}).get("name") or "未配置",
                "status": status, "note": note,
                "updated": r.get("updated_time"),
                "err_msg": r.get("err_msg"),
                "donor": donor,
            })
        self._cached_rows = out
        self._has_calculated = True
        logger.info("%s 保号状态重算完成（触发=%s，站点数=%d）", self.plugin_name, trigger, len(out))
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
