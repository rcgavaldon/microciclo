"""Microciclo backend — hosted roster and training log.

Completely separate from the Sofia apps: its own Modal app name, its own
volume, its own secret. Nothing here touches Sofia's deployment.

Data model, kept deliberately small:
  coaches/<coach>/roster.json   -> the coach's athletes
  coaches/<coach>/log/<plan>.json -> what an athlete actually lifted

Auth is a per-coach token in the URL. That is the right weight for this:
the data is training loads, not medical records, and the alternative is an
account system nobody will sign up for.
"""

import hashlib
import json
import re
import time

import modal

app = modal.App("microciclo-api")

vol = modal.Volume.from_name("microciclo-data", create_if_missing=True)
DATA = "/data"

image = modal.Image.debian_slim().pip_install("fastapi[standard]==0.115.6")

SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path(*parts: str) -> str:
    return "/".join([DATA] + list(parts))


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write(path: str, obj) -> None:
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


@app.function(image=image, volumes={DATA: vol}, region="us-east", min_containers=0)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    web = FastAPI(title="Microciclo")

    # The coach app is served from GitHub Pages, so the browser calls this
    # cross-origin. Only that origin, and only the verbs actually used.
    web.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://rcgavaldon.github.io",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

    def check(tok: str) -> str:
        if not tok or not SAFE.match(tok):
            raise HTTPException(400, "token inválido")
        return tok

    # ---- log alias: what athlete links carry instead of the master token ----
    # The share link used to embed the coach's read/write roster token, which
    # let any athlete decode it and read or overwrite the whole client list.
    # Now the roster endpoints hand back a derived write-only alias; athlete
    # links carry that, and the log endpoints resolve it. Old links with the
    # real token keep working (resolve() passes unknown tokens through).
    def _alias_of(tok: str) -> str:
        return "lg" + hashlib.sha256(("mc-log:" + tok).encode()).hexdigest()[:22]

    def _register_alias(tok: str) -> str:
        a = _alias_of(tok)
        p = _path("aliases", a)
        if _read(p, None) != tok:
            _write(p, tok)
        return a

    def resolve(tok: str) -> str:
        t = check(tok)
        real = _read(_path("aliases", t), None)
        return real if isinstance(real, str) and SAFE.match(real) else t

    @web.get("/health")
    def health():
        return {"ok": True, "t": int(time.time())}

    # ---------------- roster ----------------

    @web.get("/roster/{token}")
    def get_roster(token: str):
        t = check(token)
        vol.reload()
        alias = _register_alias(t)
        vol.commit()
        return {"atletas": _read(_path("coaches", t, "roster.json"), []),
                "media": _read(_path("coaches", t, "media.json"), {}),
                "log": alias}

    @web.post("/roster/{token}")
    async def put_roster(token: str, request: Request):
        t = check(token)
        body = await request.json()
        athletes = body.get("atletas")
        if not isinstance(athletes, list):
            raise HTTPException(400, "atletas debe ser una lista")
        if len(athletes) > 500:
            raise HTTPException(413, "demasiados atletas")
        raw = json.dumps(athletes, ensure_ascii=False)
        if len(raw) > 4_000_000:
            raise HTTPException(413, "lista demasiado grande")
        vol.reload()
        _write(_path("coaches", t, "roster.json"), athletes)
        # Optional media overrides (exercise-id -> video URL). Synced so the
        # coach's own videos follow them across devices instead of living in
        # one browser's localStorage.
        media = body.get("media")
        if isinstance(media, dict):
            clean = {}
            for k, v in list(media.items())[:100]:
                if (isinstance(k, str) and SAFE.match(k) and isinstance(v, str)
                        and v.startswith(("http://", "https://")) and len(v) <= 300):
                    clean[k] = v
            _write(_path("coaches", t, "media.json"), clean)
        alias = _register_alias(t)
        vol.commit()
        return {"ok": True, "n": len(athletes), "log": alias}

    # ---------------- training log ----------------
    # Written by the athlete's own link, read by both sides. The plan id
    # namespaces it so a new week starts clean.

    @web.get("/log/{token}/{plan}")
    def get_log(token: str, plan: str):
        t = resolve(token)
        if not SAFE.match(plan):
            raise HTTPException(400, "plan inválido")
        vol.reload()
        return _read(_path("coaches", t, "log", plan + ".json"), {})

    @web.post("/log/{token}/{plan}")
    async def put_log(token: str, plan: str, request: Request):
        t = resolve(token)
        if not SAFE.match(plan):
            raise HTTPException(400, "plan inválido")
        body = await request.json()
        entries = body.get("e")
        if not isinstance(entries, dict):
            raise HTTPException(400, "e debe ser un objeto")
        if len(json.dumps(entries)) > 500_000:
            raise HTTPException(413, "registro demasiado grande")
        vol.reload()
        p = _path("coaches", t, "log", plan + ".json")
        cur = _read(p, {})
        cur.update(entries)
        cur["_upd"] = int(time.time())
        # Which athlete this plan belongs to (a client-side hash of the name).
        # Lets /last group a player's history without ever mixing two players
        # who share the same coach token.
        ath = body.get("a") or ""
        if isinstance(ath, str) and SAFE.match(ath):
            cur["_ath"] = ath
        _write(p, cur)
        vol.commit()
        return {"ok": True, "n": sum(1 for k in cur if not k.startswith("_"))}

    # The athlete's most recent logged weight per exercise, across every past
    # plan of theirs. The app's block progression holds the load flat while
    # reps climb, so last week's weight is this week's prescription. `skip`
    # excludes the current plan so the endpoint never echoes today back.
    @web.get("/last/{token}/{ath}")
    def last_weights(token: str, ath: str, skip: str = ""):
        import os

        t = resolve(token)
        if not SAFE.match(ath):
            raise HTTPException(400, "atleta inválido")
        vol.reload()
        d = _path("coaches", t, "log")
        recs = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if not fn.endswith(".json") or fn[:-5] == skip:
                    continue
                rec = _read(os.path.join(d, fn), {})
                if rec.get("_ath") == ath:
                    recs.append(rec)
        recs.sort(key=lambda r: r.get("_upd", 0))
        out = {}
        for rec in recs:  # oldest first, so the newest write wins
            for k, v in rec.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue
                w = str(v.get("w") or "").strip()
                if w:
                    out[k.split("|")[-1]] = w[:12]
        return {"w": out}

    # Everything the coach has been sent back, newest first.
    @web.get("/logs/{token}")
    def all_logs(token: str):
        import os

        t = check(token)
        vol.reload()
        d = _path("coaches", t, "log")
        out = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    rec = _read(os.path.join(d, fn), {})
                    out.append({"plan": fn[:-5], "upd": rec.get("_upd", 0),
                                "ath": rec.get("_ath", ""),
                                "n": sum(1 for k in rec if not k.startswith("_"))})
        out.sort(key=lambda r: -r["upd"])
        return {"logs": out[:200]}

    @web.exception_handler(HTTPException)
    def _err(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return web
