#!/usr/bin/env python3
"""Живой замер бесплатных лимитов (рубрика «Халява недели», 17.10.2026).
Для каждого провайдера: не больше 2 запросов — GET каталога/ключа + ОДИН чат-запрос (max_tokens<=20, stream).
Сохраняет статус, время до первого байта/токена и заголовки, относящиеся к лимитам. Ключи в вывод не попадают."""
import json, os, re, time, urllib.request, urllib.error
from datetime import datetime, timezone

OUT = os.environ.get("OUT", "measure.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
LIMIT_RE = re.compile(r"(rate|limit|retry|quota|remaining|reset|x-ratelimit|x-request-cost|credits|x-usage)", re.I)


def load_env(*paths):
    env = {}
    for p in paths:
        try:
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    return env


ENV = {**load_env(*[p for p in os.environ.get("FREE_LLM_ENV", "").split(":") if p]), **os.environ}  # ключи: переменные окружения или файлы из FREE_LLM_ENV

P = [  # name, base, key var, list path, chat path, model, style
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "/models", "/chat/completions", "openai/gpt-oss-20b", "oai"),
    ("google", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "/models", "/chat/completions", "gemini-3.5-flash-lite", "oai"),
    ("zai", "https://api.z.ai/api/paas/v4", "ZAI_API_KEY", None, "/chat/completions", "glm-4.5-flash", "oai"),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "/models", "/chat/completions", "stepfun-ai/step-5-preview", "oai"),
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "/key", "/chat/completions", "nvidia/nemotron-3-super-120b-a12b:free", "oai"),
    ("kilo", "https://api.kilo.ai/api/gateway", None, "/models", "/chat/completions", "nvidia/nemotron-3-super-120b-a12b:free", "oai"),
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", None, "/models", "/chat/completions", "Qwen3.8-27B", "oai"),
    ("ollama", "https://ollama.com", "OLLAMA_API_KEY", "/api/tags", "/api/chat", "beam", "ollama"),
]


def limit_headers(h):
    return {k.lower(): v for k, v in h.items() if LIMIT_RE.search(k)}


def scrub(s):
    for v in ENV.values():
        if v and len(v) > 8:
            s = s.replace(v, "***")
    return s


def do(req, stream=False):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=30)
        code, hdr = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        ttfb = time.time() - t0
        return {"status": e.code, "ttfb_s": round(ttfb, 3), "limit_headers": limit_headers(dict(e.headers)),
                "error": scrub(e.read()[:300].decode("utf-8", "replace"))}
    except Exception as e:
        return {"status": None, "error": scrub(f"{type(e).__name__}: {e}")[:300]}
    ttfb = time.time() - t0
    res = {"status": code, "ttfb_s": round(ttfb, 3), "limit_headers": limit_headers(hdr)}
    ttft, text, body = None, "", b""
    try:
        with r:
            for raw in r:
                body += raw
                if not stream:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                piece = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content") \
                    if "choices" in ev else (ev.get("message") or {}).get("content")
                d0 = ((ev.get("choices") or [{}])[0].get("delta") or {}) if "choices" in ev else (ev.get("message") or {})
                if (piece or d0.get("reasoning") or d0.get("reasoning_content") or d0.get("thinking")) and ttft is None:
                    ttft = time.time() - t0   # first token, including hidden reasoning (max_tokens=20 is often all reasoning)
                if piece:
                    if ttft is None:
                        ttft = time.time() - t0
                    text += str(piece)
    except Exception as e:
        res["read_error"] = f"{type(e).__name__}: {e}"[:200]
    res["wall_s"] = round(time.time() - t0, 3)
    if stream:
        res["ttft_s"] = round(ttft, 3) if ttft else None
        res["text"] = text.strip()[:120]
    else:
        try:
            j = json.loads(body)
            if isinstance(j, dict) and "data" in j and isinstance(j["data"], list):
                res["models_count"] = len(j["data"])
            elif isinstance(j, dict) and "models" in j:
                res["models_count"] = len(j["models"])
            elif isinstance(j, dict) and isinstance(j.get("data"), dict):   # OpenRouter /key
                d = j["data"]
                res["key_info"] = {k: d.get(k) for k in ("is_free_tier", "limit", "limit_remaining", "usage",
                                                          "usage_daily", "rate_limit") if k in d}
        except ValueError:
            res["body_head"] = scrub(body[:200].decode("utf-8", "replace"))
    return res


def main():
    out = {"measured_at": datetime.now(timezone.utc).isoformat(), "providers": []}
    for name, base, keyvar, lpath, cpath, model, style in P:
        hdr = {"User-Agent": UA, "Content-Type": "application/json"}
        row = {"provider": name, "model": model, "base": base}
        if keyvar:
            k = ENV.get(keyvar)
            if not k:
                row["error"] = f"нет ключа {keyvar}"
                out["providers"].append(row)
                continue
            hdr["Authorization"] = f"Bearer {k}"
        if lpath:
            row["list"] = do(urllib.request.Request(base + lpath, headers=hdr))
        if style == "ollama":
            body = {"model": model, "messages": [{"role": "user", "content": "Скажи «привет»."}], "stream": True,
                    "think": False, "options": {"num_predict": 20}}
        else:
            body = {"model": model, "messages": [{"role": "user", "content": "Скажи «привет»."}], "stream": True,
                    "max_tokens": 20, "temperature": 0}
            if name == "google":
                body["reasoning_effort"] = "low"
            if name == "nvidia":
                body["chat_template_kwargs"] = {"enable_thinking": False, "thinking": False}
        row["chat"] = do(urllib.request.Request(base + cpath, data=json.dumps(body).encode(), headers=hdr), stream=True)
        out["providers"].append(row)
        print(name, row["chat"].get("status"), row["chat"].get("ttft_s"), row["chat"].get("limit_headers"), flush=True)
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
