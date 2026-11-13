"""Serve API docs and the local Swagger request interceptor."""

import json
import struct

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, Response

_INTERCEPTOR = r"""
async function projectHubRequest(request) {
    function canonical(value) {
        if (typeof value === "string" &&
            /(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]|[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(value)) {
            throw new Error("data 包含无效 Unicode 字符。");
        }
        if (typeof value === "number" && !Number.isFinite(value)) {
            throw new Error("data 包含非有限数字，无法计算 sha256。");
        }
        if (value === null || typeof value !== "object") return JSON.stringify(value);
        if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
        const fields = Object.keys(value).sort().map(key => canonical(key) + ":" + canonical(value[key]));
        return "{" + fields.join(",") + "}";
    }

    // The SHA-256 initial words and round constants derive from prime roots (FIPS 180-4).
    // Web Crypto may be unavailable on the service's default HTTP listener.
    function digestWithoutWebCrypto(input) {
        const primes = [];
        for (let candidate = 2; primes.length < 64; candidate++) {
            if (primes.every(prime => candidate % prime !== 0)) primes.push(candidate);
        }
        const wordsFromRoots = (values, root) => Uint32Array.from(values, prime => {
            const value = root(prime);
            return (value % 1) * 2 ** 32;
        });
        const state = wordsFromRoots(primes.slice(0, 8), Math.sqrt);
        const constants = wordsFromRoots(primes, Math.cbrt);
        const message = new Uint8Array(Math.ceil((input.length + 9) / 64) * 64);
        message.set(input);
        message[input.length] = 0x80;
        const view = new DataView(message.buffer);
        view.setBigUint64(message.length - 8, BigInt(input.length) * 8n);
        const rotate = (word, count) => (word >>> count) | (word << (32 - count));
        const schedule = new Uint32Array(64);

        for (let offset = 0; offset < message.length; offset += 64) {
            for (let index = 0; index < 16; index++) schedule[index] = view.getUint32(offset + 4 * index);
            for (let index = 16; index < 64; index++) {
                const first = schedule[index - 15];
                const second = schedule[index - 2];
                const low = rotate(first, 7) ^ rotate(first, 18) ^ (first >>> 3);
                const high = rotate(second, 17) ^ rotate(second, 19) ^ (second >>> 10);
                schedule[index] = schedule[index - 16] + (low >>> 0) + schedule[index - 7] + (high >>> 0);
            }
            const round = [...state];
            for (let index = 0; index < 64; index++) {
                const [a, b, c, d, e, f, g, h] = round;
                const spreadE = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25);
                const spreadA = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22);
                const choose = g ^ (e & (f ^ g));
                const majority = (a & b) | (c & (a | b));
                const next = (h + spreadE + choose + constants[index] + schedule[index]) >>> 0;
                round.pop();
                round.unshift((next + spreadA + majority) >>> 0);
                round[4] = (next + d) >>> 0;
            }
            round.forEach((word, index) => { state[index] += word; });
        }
        return Array.from(state, word => word.toString(16).padStart(8, "0")).join("");
    }

    async function digest(data) {
        const bytes = new TextEncoder().encode(canonical(data));
        if (globalThis.crypto?.subtle) {
            const result = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
            return Array.from(result, byte => byte.toString(16).padStart(2, "0")).join("");
        }
        return digestWithoutWebCrypto(bytes);
    }

    // JSON.parse checks the grammar. Find only top-level digest values in the original text
    // so arbitrary integer IDs and the exact data literals survive sending.
    function insertDigest(body, value) {
        const tokens = /"(?:[^"\\]|\\[\s\S])*"|[{}\[\]:,]|[^\s{}\[\]:,"]+/g;
        const ranges = [];
        let depth = 0;
        let key, previous, start, end, close;
        for (const match of body.matchAll(tokens)) {
            const [token] = match;
            if (depth === 1) {
                if (previous === "{" || previous === ",") key = JSON.parse(token);
                if (previous === ":" && key === "sha256") start = match.index;
                if ((token === "," || token === "}") && start !== undefined) {
                    ranges.push([start, end]);
                    start = undefined;
                }
            }
            if (token === "{" || token === "[") depth++;
            if (token === "}" || token === "]") depth--;
            if (depth === 0 && token === "}") close = match.index;
            previous = token;
            end = match.index + token.length;
        }
        if (!ranges.length) return body.slice(0, close) + ',"sha256":' + value + body.slice(close);
        for (const [begin, finish] of ranges.reverse()) {
            body = body.slice(0, begin) + value + body.slice(finish);
        }
        return body;
    }

    const headers = Object.fromEntries(Object.entries(request.headers || {}).map(([key, value]) => {
        const credential = ["x-user-name", "x-password"].includes(key.toLowerCase());
        return [key, credential && typeof value === "string" ? encodeURIComponent(value) : value];
    }));
    const prepared = {...request, headers};
    const method = (request.method || "").toUpperCase();
    const path = (request.url || "").split(/[?#]/, 1)[0].replace(/\/$/, "");
    const isUpload = /\/(curves|firmwares|internal-files|public-files)\/upload$/.test(path);
    const isCurveReplacement = method === "PUT" && /\/curves\/\d+$/.test(path);
    if (isUpload || isCurveReplacement || !["POST", "PUT", "PATCH"].includes(method)
        || typeof request.body !== "string") return prepared;

    let payload;
    try {
        payload = JSON.parse(request.body);
    } catch {
        return prepared;
    }
    if (payload && !Array.isArray(payload) && Object.prototype.hasOwnProperty.call(payload, "data")) {
        prepared.body = insertDigest(request.body, JSON.stringify(await digest(payload.data)));
    }
    return prepared;
}
"""


def _make_icon() -> bytes:
    """Draw a 16x16 RGBA icon encoded as an ICO bitmap."""
    size = 16
    background = bytes((0x52, 0x38, 0x26, 0xFF))
    foreground = bytes((0xB3, 0xD7, 0xFF, 0xFF))
    strokes = {(x, y) for y in range(4, 12) for x in range(4, 12) if x in (4, 5, 10, 11) or y in (7, 8)}
    pixels = b"".join(
        foreground if (x, y) in strokes else background for y in reversed(range(size)) for x in range(size)
    )
    mask = bytes(size * 4)
    bitmap = struct.pack("<IiiHHIIiiII", 40, size, 2 * size, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    image = bitmap + pixels + mask
    directory = struct.pack("<HHHBBBBHHII", 0, 1, 1, size, size, 0, 0, 1, 32, len(image), 22)
    return directory + image


_ICON = _make_icon()
_MARKER = "PROJECTHUB_SWAGGER_INTERCEPTOR"


def register_docs(app: FastAPI) -> None:
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(_ICON, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request):
        root = request.scope.get("root_path", "").rstrip("/")
        parameters = {**(app.swagger_ui_parameters or {}), "requestInterceptor": _MARKER}
        html = get_swagger_ui_html(
            openapi_url=f"{root}{app.openapi_url}",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=f"{root}{app.swagger_ui_oauth2_redirect_url}",
            init_oauth=app.swagger_ui_init_oauth,
            swagger_ui_parameters=parameters,
            swagger_favicon_url=f"{root}/favicon.ico",
        ).body.decode("utf-8")
        expression = f'"requestInterceptor": {json.dumps(_MARKER)}'
        if html.count(expression) != 1:
            raise RuntimeError("Swagger 模板未找到请求拦截器位置。")
        return HTMLResponse(html.replace(expression, f'"requestInterceptor": {_INTERCEPTOR}', 1))

    @app.get("/redoc", include_in_schema=False)
    async def redoc(request: Request):
        root = request.scope.get("root_path", "").rstrip("/")
        return get_redoc_html(
            openapi_url=f"{root}{app.openapi_url}",
            title=f"{app.title} - ReDoc",
            redoc_favicon_url=f"{root}/favicon.ico",
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def oauth2_redirect():
        return get_swagger_ui_oauth2_redirect_html()
