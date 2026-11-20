"""职汇优培 · AI 职发助手（Streamlit 版）

和网页版共用同一套提示词与档案规则，改动的只有界面层：
- 对话在服务端调用模型接口，浏览器不直接访问上游，所以没有跨域问题
- 档案按「8 项清单」推进，每轮后台自动整理一次
- 只用学生亲口说过的内容，找不到原话的能力条目不写进档案
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid

import requests
import streamlit as st

BASE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE, "content.json"), encoding="utf-8") as fh:
    CONTENT = json.load(fh)

SYS = CONTENT["SYS"]
GREETING = CONTENT["GREETING"]
# 站点里的 MODES 用 id/label，这里统一成 key/name 方便取用
MODES = [
    {"key": m.get("key") or m.get("id"), "name": m.get("name") or m.get("label") or m.get("id"),
     "desc": m.get("desc", "")}
    for m in CONTENT["MODES"]
]
SYS_EXTRACT = CONTENT["SYS_EXTRACT"]
CAPABILITIES = CONTENT["CAPABILITIES"]
PHASES = CONTENT["PHASES"]

DEFAULT_ACCESS_KEY = "wbpk_s0FIbXFb9SjrXPoYC35U9n_DMtpQ9eZPN01AcxY7E935nBpbFfy3NSx"
DEFAULT_UPSTREAM = "https://zhihui-helper.app.workbuddy.host"
DEFAULT_MODEL = "hunyuan-chat"

_CONF_CACHE: dict[str, str] = {}


def _conf(key: str, default: str = "") -> str:
    """按「环境变量 → Streamlit secrets → 默认值」的顺序取配置。

    顺序是先环境变量：这样模块导入阶段完全不碰 st.secrets，
    避免在 set_page_config 之前触发 Streamlit 的调用顺序限制。
    Streamlit Community Cloud 在应用设置里填的 Secrets 会在第一次真正
    需要配置时（发消息）才被读取，那时页面早初始化完了。
    """
    if key in _CONF_CACHE:
        return _CONF_CACHE[key]

    val = (os.environ.get(key) or "").strip()
    if not val:
        try:
            val = str(st.secrets.get(key, "") or "").strip()
        except Exception:
            val = ""
    if not val:
        val = (default or "").strip()

    _CONF_CACHE[key] = val
    return val


def _upstream() -> str:
    return _conf("ZHIHUI_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


def _chat_url() -> str:
    return _upstream() + "/.cloud/llm/chat/completions"


def _model() -> str:
    return _conf("ZHIHUI_MODEL", DEFAULT_MODEL)


def _headers() -> dict:
    return {
        "content-type": "application/json",
        "x-wb-webapp-access-key": _conf("ZHIHUI_ACCESS_KEY", DEFAULT_ACCESS_KEY),
        "accept": "text/event-stream",
    }

MAX_HISTORY = 12
MAX_MSG_CHARS = 1200

META_WORDS = ["观察", "接住", "追问", "过渡", "策略", "维度", "隐含", "采集", "画像",
              "智能体", "Agent", "接下来", "顺着", "下一步", "备注", "内心", "思考过程", "分析"]
AGENDA_LINE = re.compile(r"(本轮议程|内部提纲|内部备注|已拿到|还缺|这一轮(只)?推进|议程)")

ARR_LIMIT = {"internships": 4, "projects": 5, "campus": 5, "competitions": 4,
             "skills": 8, "certificates": 6, "followUps": 6, "gaps": 5}

# ---------------------------------------------------------------- 档案结构


def empty_profile() -> dict:
    return {
        "major": "", "level": "", "targetIndustry": "", "targetRole": "", "confusion": "",
        "internships": [], "projects": [], "campus": [], "competitions": [],
        "skills": [], "certificates": [],
        "talkStyle": "", "styleSignals": [],
        "capabilities": [], "logs": [], "tags": [],
        "updatedAt": dt.datetime.now().isoformat(timespec="seconds"),
    }


def sv(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def av(v) -> list:
    return [x for x in v if x] if isinstance(v, list) else []


def cap_name(key: str) -> str:
    for c in CAPABILITIES:
        if c["key"] == key:
            return c["name"]
    return key


NEEDS = [
    {"id": "basic", "name": "专业与年级", "probe": "读什么专业、大几",
     "has": lambda p: bool(sv(p.get("major")) and sv(p.get("level")))},
    {"id": "project", "name": "课程项目", "probe": "哪门课做过什么作业或项目，他具体负责哪块",
     "has": lambda p: len(av(p.get("projects"))) > 0},
    {"id": "campus", "name": "社团或学生工作", "probe": "社团、学生会、班委里做过什么具体的事",
     "has": lambda p: len(av(p.get("campus"))) > 0},
    {"id": "intern", "name": "实习或兼职", "probe": "实习兼职、门店打工、家教、助管都算，做过什么",
     "has": lambda p: len(av(p.get("internships"))) > 0},
    {"id": "competition", "name": "竞赛或比赛", "probe": "参加过什么比赛，校内的小比赛也算，结果如何",
     "has": lambda p: len(av(p.get("competitions"))) > 0},
    {"id": "skill", "name": "技能与证书", "probe": "会用的工具、软件、外语，考过什么证",
     "has": lambda p: len(av(p.get("skills"))) + len(av(p.get("certificates"))) > 0},
    {"id": "target", "name": "想去的行业或岗位", "probe": "想去什么行业、投什么岗位，有没有具体的公司",
     "has": lambda p: bool(sv(p.get("targetIndustry")) or sv(p.get("targetRole")))},
    {"id": "confusion", "name": "现在的困惑", "probe": "现在最卡住他的是什么——投不出去、不会写、还是不知道选什么",
     "has": lambda p: bool(sv(p.get("confusion")))},
]


def coverage(profile: dict):
    missing = [n for n in NEEDS if not n["has"](profile)]
    return {"done": len(NEEDS) - len(missing), "missing": missing}


def agenda_block(profile: dict) -> str:
    cov = coverage(profile)
    if not cov["missing"]:
        return ("\n\n（内部备忘，绝对不许出现在回复里：八项都聊到了。跟他说一句「这块差不多了」，"
                "再问他想先解决哪件事——看方向 / 改简历 / 换个主题再聊一段。）")
    need = cov["missing"][0]
    rest = "、".join(n["name"] for n in cov["missing"][1:4])
    tail = f"后面还要补：{rest}。" if rest else ""
    return (
        "\n\n（内部备忘，绝对不许出现在回复里：这一轮顺着他的话，把「"
        + need["name"]
        + "」问出来——要问到的是："
        + need["probe"]
        + "。"
        + tail
        + "一次只问一项，别提「议程」「清单」「档案还差」这类字眼。）"
    )


def profile_block(profile: dict) -> str:
    slim = {k: v for k, v in profile.items() if k not in ("logs", "tags")}
    return (
        "【我的经历档案（结构化，可能不完整）】\n"
        + json.dumps(slim, ensure_ascii=False, indent=1)
        + "\n\n【关于 styleSignals 的使用限制】这些是聊天过程中对他做事方式的观察，不是心理测评，"
        "也不一定准确。引用时必须写明「这是聊天中的观察」；只使用 confidence 为「高」或「中」的条目；"
        "只能作为参考维度，不能单独决定匹配与否。"
    )


# ---------------------------------------------------------------- 文本净化


def strip_agenda(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if re.match(r"^【.{0,12}(议程|提纲|备注)", s):
            continue
        if AGENDA_LINE.match(s) and len(s) < 40:
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def strip_parens(text: str) -> str:
    def repl(m):
        inner = m.group(1)
        return "" if len(inner) >= 18 or any(w in inner for w in META_WORDS) else m.group(0)

    return re.sub(r"[（(]([^（()）]{0,80})[)）]", repl, text)


def sanitize(text: str) -> str:
    return strip_parens(strip_agenda(text)).strip()


# ---------------------------------------------------------------- 档案合并


def norm_entry(v) -> str:
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v).strip()
    if isinstance(v, dict):
        parts = [v.get(k) for k in ("name", "title", "role", "description", "detail", "value")]
        return " · ".join(str(x).strip() for x in parts if x)
    return str(v).strip()


def uniq_arr(items, limit: int) -> list:
    seen, out = set(), []
    for it in items or []:
        text = norm_entry(it)
        if not text:
            continue
        key = re.sub(r"[\s，。、；,.!?！？:：\"'「」『』]", "", text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def merge_profile(old: dict, new: dict) -> dict:
    merged = dict(old)
    scalars = ("major", "level", "targetIndustry", "targetRole", "confusion", "talkStyle")
    for k in scalars:
        if sv(new.get(k)):
            merged[k] = sv(new[k])
    for k, limit in ARR_LIMIT.items():
        merged[k] = uniq_arr(new.get(k), limit) or uniq_arr(old.get(k), limit)
    new_sig = new.get("styleSignals") or []
    merged["styleSignals"] = new_sig if new_sig else old.get("styleSignals", [])
    merged["updatedAt"] = dt.datetime.now().isoformat(timespec="seconds")
    return merged


# ---------------------------------------------------------------- 模型调用


def _post_chat(messages, *, stream: bool, temperature: float, max_tokens: int, json_mode: bool = False,
               timeout: int = 120):
    payload = {
        "model": _model(),
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "conversationId": "studio-" + uuid.uuid4().hex[:12],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = requests.post(_chat_url(), headers=_headers(), data=json.dumps(payload), timeout=timeout, stream=stream)
    # 上游 SSE 不带 charset，requests 会按 ISO-8859-1 解，中文会变乱码 —— 强制按 UTF-8
    resp.encoding = "utf-8"
    return resp


def chat_stream(messages):
    """逐段吐出模型正文。"""
    try:
        resp = _post_chat(messages, stream=True, temperature=0.7, max_tokens=700)
    except Exception as exc:  # 网络层
        yield "（连不上模型服务：" + str(exc)[:80] + "）"
        return
    if resp.status_code != 200:
        yield "（模型服务返回 " + str(resp.status_code) + "，稍后再试一次）"
        return

    buf = ""
    for raw in resp.iter_lines(decode_unicode=False):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if obj.get("error"):
            yield "\n（出错：" + str(obj["error"])[:60] + "）"
            return
        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
        piece = delta.get("content") or ""
        if piece:
            buf += piece
            yield piece
    if not buf.strip():
        yield "（这次没生成正文，麻烦你再发一次）"


def call_json(system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 1200) -> dict:
    """上游只接受 stream=true，所以这里走流式再把正文拼起来解析 JSON。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        resp = _post_chat(messages, stream=True, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        if resp.status_code != 200:
            return {}
        parts = []
        for raw in resp.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                parts.append(piece)
        text = "".join(parts).strip()
    except Exception:
        return {}
    if "```" in text:
        text = re.sub(r"```[a-zA-Z]*", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def run_extract(transcript: str) -> dict:
    if len(transcript) < 20:
        return {}
    data = call_json(SYS_EXTRACT, "对话记录：\n" + transcript[:6000])
    if not data:
        return {}
    out = {}
    for k in ("major", "level", "targetIndustry", "confusion", "talkStyle"):
        if sv(data.get(k)):
            out[k] = sv(data[k])
    for k in ("internships", "projects", "campus", "competitions", "skills", "certificates"):
        if isinstance(data.get(k), list):
            out[k] = uniq_arr(data[k], ARR_LIMIT[k])
    sigs = data.get("styleSignals")
    if isinstance(sigs, list) and sigs:
        cleaned, seen = [], set()
        for s in sigs:
            if not isinstance(s, dict):
                continue
            ev = sv(s.get("evidence"))
            if ev in seen:
                continue
            seen.add(ev)
            cleaned.append({
                "dimension": sv(s.get("dimension")),
                "observation": sv(s.get("observation")),
                "evidence": ev,
                "confidence": s.get("confidence") if s.get("confidence") in ("高", "中", "低") else "中",
            })
        out["styleSignals"] = cleaned[:5]
    return out


# ---------------------------------------------------------------- 界面

st.set_page_config(page_title="职汇优培 · AI 职发助手", page_icon="💬", layout="wide")

st.markdown(
    """
<style>
:root{--brand:#1F4E79;--accent:#2E75B6;--soft:#F6F8FB;--line:#E4EAF1;--muted:#6B7A8C;--ink:#101A24}
.block-container{padding-top:2.2rem;max-width:1180px}
h1,h2,h3{color:var(--ink)}
.zyp-title{font-size:26px;font-weight:700;color:var(--brand);margin:0}
.zyp-sub{color:var(--muted);font-size:13px;margin:2px 0 10px}
.zyp-chip{display:inline-block;background:#EAF2FB;color:var(--brand);border-radius:999px;
  padding:2px 10px;font-size:12px;margin:0 6px 6px 0}
.zyp-chip-off{background:#F1F4F8;color:#93A1B0}
.zyp-card{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.zyp-card b{font-size:13px;color:var(--muted);font-weight:600}
.zyp-note{color:var(--muted);font-size:12px;line-height:1.7}
div[data-testid="stChatMessage"]{background:#fff;border:1px solid var(--line);border-radius:12px}
section[data-testid="stSidebar"]{background:var(--soft)}
footer,.zyp-foot{color:var(--muted);font-size:12px}
</style>
""",
    unsafe_allow_html=True,
)

if "profile" not in st.session_state:
    st.session_state.profile = empty_profile()
if "history" not in st.session_state:
    st.session_state.history = []
if "mode" not in st.session_state:
    st.session_state.mode = MODES[0]["key"]

mode_keys = [m["key"] for m in MODES]
mode_names = {m["key"]: m["name"] for m in MODES}
mode_descs = {m["key"]: m.get("desc", "") for m in MODES}

# ---------------- 侧栏：我的经历档案
with st.sidebar:
    st.markdown('<div class="zyp-title">我的经历档案</div>', unsafe_allow_html=True)
    cov = coverage(st.session_state.profile)
    st.progress(cov["done"] / len(NEEDS), text=f"已聊到 {cov['done']} / {len(NEEDS)} 项")
    if cov["missing"]:
        st.caption("还差：" + "、".join(n["name"] for n in cov["missing"]))
    else:
        st.caption("八项都聊到了，可以让档案再厚一点。")

    chips = "".join(
        f'<span class="zyp-chip{"" if n["has"](st.session_state.profile) else " zyp-chip-off"}">{n["name"]}</span>'
        for n in NEEDS
    )
    st.markdown(chips, unsafe_allow_html=True)
    if st.session_state.get("extract_note"):
        st.caption(st.session_state["extract_note"])

    p = st.session_state.profile
    rows = [
        ("专业 / 年级", " · ".join(x for x in (sv(p["major"]), sv(p["level"])) if x)),
        ("实习 / 兼职", "；".join(av(p["internships"]))),
        ("课程项目", "；".join(av(p["projects"]))),
        ("社团学生工作", "；".join(av(p["campus"]))),
        ("竞赛", "；".join(av(p["competitions"]))),
        ("技能", "；".join(av(p["skills"]))),
        ("证书", "；".join(av(p["certificates"]))),
        ("想去的方向", " · ".join(x for x in (sv(p["targetIndustry"]), sv(p["targetRole"])) if x)),
        ("现在的困惑", sv(p["confusion"])),
    ]
    for label, value in rows:
        shown = value if value else '<span style="color:#A8B4C0">未提供</span>'
        st.markdown(f'<div class="zyp-card"><b>{label}</b><br>{shown}</div>', unsafe_allow_html=True)

    sigs = p.get("styleSignals") or []
    if sigs:
        st.markdown("**做事风格（观察）**")
        for s in sigs[:6]:
            conf = s.get("confidence", "")
            obs = sv(s.get("observation")) or "未提供"
            ev = sv(s.get("evidence"))
            st.markdown(
                f'<div class="zyp-card"><b>{sv(s.get("dimension")) or "观察"} · {conf}</b><br>{obs}'
                + (f'<br><span class="zyp-note">依据你说的：{ev}</span>' if ev else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        st.caption("来自聊天中的观察，不是测评，仅供参考。")

    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            "导出档案",
            data=json.dumps(st.session_state.profile, ensure_ascii=False, indent=2),
            file_name=f"我的经历档案_{dt.date.today()}.json",
            mime="application/json",
            use_container_width=True,
        )
    with col_b:
        if st.button("删除档案", use_container_width=True):
            st.session_state.profile = empty_profile()
            st.rerun()

# ---------------- 主区
st.markdown('<div class="zyp-title">职汇优培 · AI 职发助手</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="zyp-sub">主题你来定，提纲我来记 —— 助手心里有一张档案清单，还缺哪项就往哪项问。</div>',
    unsafe_allow_html=True,
)

mode = st.radio(
    "模式",
    mode_keys,
    index=mode_keys.index(st.session_state.mode) if st.session_state.mode in mode_keys else 0,
    format_func=lambda k: mode_names[k],
    horizontal=True,
    label_visibility="collapsed",
)
if mode != st.session_state.mode:
    st.session_state.mode = mode
st.caption(mode_descs.get(mode, ""))

if not st.session_state.history:
    st.session_state.history.append({"role": "assistant", "content": GREETING.get(mode, "")})

for msg in st.session_state.history:
    with st.chat_message("assistant" if msg["role"] == "assistant" else "user"):
        st.markdown(msg["content"])

prompt = st.chat_input("说点什么…（答不上来直接说「跳过」，我换个问法）")

if prompt:
    st.session_state.history.append({"role": "user", "content": prompt[:MAX_MSG_CHARS]})
    with st.chat_message("user"):
        st.markdown(prompt[:MAX_MSG_CHARS])

    sys_text = SYS.get(mode, "")
    if mode == "dig":
        sys_text += agenda_block(st.session_state.profile)
    messages = [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": profile_block(st.session_state.profile)},
    ] + [{"role": m["role"], "content": m["content"]} for m in st.session_state.history[-MAX_HISTORY:]]

    with st.chat_message("assistant"):
        raw = st.write_stream(chat_stream(messages))

    reply = sanitize(raw or "")
    st.session_state.history.append({"role": "assistant", "content": reply})
    st.session_state.pending_extract = mode == "dig" and len(reply) > 0
    st.rerun()

# 后台整理：每轮对话后自动把新说到的内容并进档案
if st.session_state.pop("pending_extract", False):
    history = st.session_state.history
    transcript = "\n".join(
        ("学生：" if m["role"] == "user" else "助手：") + m["content"] for m in history[-8:]
    )
    with st.spinner("正在整理进你的档案…"):
        delta = run_extract(transcript)
    if delta:
        st.session_state.profile = merge_profile(st.session_state.profile, delta)
        st.session_state["extract_note"] = ""
    else:
        st.session_state["extract_note"] = "这轮没整理出新的内容，多说两句细节就能进档案。"
    st.rerun()

st.divider()
st.markdown(
    '<div class="zyp-foot">本页输出由人工智能生成，仅供参考，<b>不构成任何就业、录用或薪酬承诺</b>。'
    "涉及招聘信息、薪资、编制等一切与就业相关的结论，请以企业官方渠道与学校就业指导部门的正式通知为准。"
    "请不要填写姓名、手机号、身份证号、学校班级等个人信息 —— 整理档案时这类信息会被自动丢弃。</div>",
    unsafe_allow_html=True,
)
