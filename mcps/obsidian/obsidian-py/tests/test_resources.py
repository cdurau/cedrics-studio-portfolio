import asyncio
import base64
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from obsidian_mcp.config import ServerConfig, VaultConfig
from obsidian_mcp.resources import note_uri, parse_uri
from obsidian_mcp.server import build_server
from obsidian_mcp.vault import Vault, VaultError

from test_vault import FILES, make_vault


@pytest.fixture
def server(tmp_path):
    vault_dir = tmp_path / "Mein Vault"
    vault_dir.mkdir()
    files = dict(FILES)
    files["Journal/2026-09-26.md"] = "# Tag\n## Log\n- eins\n"
    make_vault(vault_dir, files, obsidian={"daily-notes.json": {"folder": "Journal"}})
    cfg = ServerConfig(vaults={"privat": VaultConfig(name="privat", path=vault_dir)})
    return build_server(cfg), vault_dir


def run(server, fn):
    async def main():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await fn(client)
    return asyncio.run(main())


def text_of(result):
    return result.contents[0].text


def test_parse_uri_variants(tmp_path):
    vaults = {"privat": Vault(VaultConfig(name="privat", path=tmp_path))}
    assert parse_uri("obsidian://privat/", vaults).kind == "vault"
    p = parse_uri("obsidian://privat/Ordner%20A/Notiz%20%C3%84.md#Meine%20%C3%9Cberschrift", vaults)
    assert (p.kind, p.arg, p.fragment) == ("file", "Ordner A/Notiz Ä.md", "Meine Überschrift")
    assert parse_uri("obsidian://privat/Ordner/", vaults).kind == "folder"
    assert parse_uri("obsidian://privat/.daily/gestern", vaults).arg == "gestern"
    assert parse_uri("obsidian://privat/.tag/projekt/beta", vaults).arg == "projekt/beta"
    native = parse_uri(f"obsidian://open?vault={tmp_path.name}&file=Ordner%2FNotiz", vaults)
    assert (native.vault, native.kind, native.arg) == ("privat", "file", "Ordner/Notiz")
    with pytest.raises(VaultError):
        parse_uri("obsidian://gibtsnicht/x.md", vaults)
    assert note_uri("privat", "Ordner A/Ä.md", "Kopf") == "obsidian://privat/Ordner%20A/%C3%84.md#Kopf"


def test_list_resources_and_templates(server):
    srv, _ = server

    async def fn(client):
        res = await client.list_resources()
        tpl = await client.list_resource_templates()
        return res, tpl

    res, tpl = run(srv, fn)
    uris = [str(r.uri) for r in res.resources]
    assert "obsidian://privat/" in uris
    assert "obsidian://privat/Projekte/Alpha.md" in uris
    assert "obsidian://privat/.daily/today" in uris
    assert {t.name for t in tpl.resourceTemplates} == {"note", "folder", "daily-note", "tag"}


def test_read_resources(server):
    srv, _ = server

    async def fn(client):
        out = {}
        for key, uri in {
            "note": "obsidian://privat/Projekte/Alpha.md",
            "section": "obsidian://privat/Projekte/Alpha.md#Aufgaben",
            "noext": "obsidian://privat/Beta",
            "root": "obsidian://privat/",
            "folder": "obsidian://privat/Projekte/",
            "tag": "obsidian://privat/.tag/projekt",
            "daily": "obsidian://privat/.daily/2026-09-26",
            "native": "obsidian://open?vault=Mein%20Vault&file=Projekte%2FAlpha",
            "bin": "obsidian://privat/bild.png",
        }.items():
            out[key] = await client.read_resource(uri)
        return out

    r = run(srv, fn)
    assert text_of(r["note"]).startswith("# Alpha\n")
    assert text_of(r["section"]).strip() == "## Aufgaben\n- a"
    assert "Beta Inhalt" in text_of(r["noext"])
    assert "## Unterordner" in text_of(r["root"]) and "[Projekte/](obsidian://privat/Projekte/)" in text_of(r["root"])
    assert "[Alpha](obsidian://privat/Projekte/Alpha.md)" in text_of(r["folder"])
    assert "[Beta](obsidian://privat/Beta.md)" in text_of(r["tag"])
    assert "- eins" in text_of(r["daily"])
    assert text_of(r["native"]).startswith("# Alpha")
    assert base64.b64decode(r["bin"].contents[0].blob) == b"PNG"
    assert r["bin"].contents[0].mimeType == "image/png"


def test_resource_errors(server):
    srv, _ = server

    async def fn(client):
        errors = []
        for uri in ["obsidian://privat/Fehlt.md", "obsidian://privat/.daily/1999-01-01",
                    "obsidian://privat/../geheim.md", "obsidian://privat/.obsidian/app.json"]:
            try:
                await client.read_resource(uri)
                errors.append(None)
            except Exception as exc:  # McpError
                errors.append(str(exc))
        return errors

    errors = run(srv, fn)
    assert all(errors), errors


def test_tools_accept_obsidian_uris(server):
    srv, vault_dir = server

    async def fn(client):
        a = await client.call_tool("read_note", {"path": "obsidian://privat/Projekte/Alpha.md#Notizen"})
        b = await client.call_tool("append_to_note", {"path": "obsidian://privat/.daily/2026-09-26#Log",
                                                      "content": "- zwei"})
        c = await client.call_tool("get_backlinks", {"path": "obsidian://open?vault=Mein%20Vault&file=Beta"})
        return a, b, c

    a, b, c = run(srv, fn)
    assert json.loads(a.content[0].text)["content"].strip() == "## Notizen\nx"
    assert not b.isError
    assert (vault_dir / "Journal/2026-09-26.md").read_text().endswith("## Log\n- eins\n- zwei\n")
    assert not c.isError and "Index.md" in c.content[0].text


def test_list_results_are_single_json_objects(server):
    srv, _ = server

    async def fn(client):
        tags = await client.call_tool("list_tags", {})
        unresolved = await client.call_tool("find_unresolved_links", {})
        empty = await client.call_tool("find_notes_by_tag", {"tag": "gibtsnicht"})
        back = await client.call_tool("get_backlinks", {"path": "Beta"})
        return tags, unresolved, empty, back

    for res in run(srv, fn):
        assert not res.isError and len(res.content) == 1
    tags, unresolved, empty, back = (json.loads(r.content[0].text) for r in run(srv, fn))
    assert tags["total"] == len(tags["tags"]) > 0
    assert unresolved["total"] == 1 and unresolved["unresolved"][0]["target"] == "Fehlt"
    assert empty == {"total": 0, "tag": "gibtsnicht", "notes": []}
    assert back["path"] == "Beta.md" and back["total"] == len(back["backlinks"]) >= 1


def test_replace_section_tool_with_uri_fragment(server):
    srv, vault_dir = server

    async def fn(client):
        dry = await client.call_tool("replace_section", {"path": "obsidian://privat/Projekte/Alpha.md#Aufgaben",
                                                         "content": "- x", "dry_run": True})
        real = await client.call_tool("replace_section", {"path": "Projekte/Alpha", "heading": "Aufgaben",
                                                          "content": "- x"})
        missing = await client.call_tool("replace_section", {"path": "Projekte/Alpha", "content": "- x"})
        return dry, real, missing

    dry, real, missing = run(srv, fn)
    assert json.loads(dry.content[0].text)["dry_run"] is True
    assert not real.isError
    assert "## Aufgaben\n- x\n\n## Notizen" in (vault_dir / "Projekte/Alpha.md").read_text()
    assert missing.isError


def test_http_auth_rejects_query_token(tmp_path):
    from starlette.testclient import TestClient

    from obsidian_mcp.server import build_http_app

    vault_dir = tmp_path / "v"
    vault_dir.mkdir()
    cfg = ServerConfig(vaults={"v": VaultConfig(name="v", path=vault_dir)}, token="geheim")
    with TestClient(build_http_app(cfg)) as http:
        assert http.get("/health").status_code == 200
        assert http.post("/mcp?token=geheim", json={}).status_code == 401
        assert http.post("/mcp", json={}, headers={"Authorization": "Bearer falsch"}).status_code == 401
        ok = http.post("/mcp", headers={"Authorization": "Bearer geheim", "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert ok.status_code != 401


def test_http_auth_cloudflare_access_jwt(tmp_path):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from starlette.testclient import TestClient

    from obsidian_mcp.server import BearerAuth, CfAccessVerifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeJWKS:  # stands in for PyJWKClient: no network access in tests
        def get_signing_key_from_jwt(self, _token):
            return type("K", (), {"key": key.public_key()})()

    verifier = CfAccessVerifier("team", "aud-tag", jwks_client=FakeJWKS())
    assert verifier.issuer == "https://team.cloudflareaccess.com"

    def token(signing_key=key, **over):
        now = int(time.time())
        claims = {"iss": verifier.issuer, "aud": ["aud-tag"], "iat": now, "exp": now + 300,
                  "email": "user@example.com"} | over
        return jwt.encode(claims, signing_key, algorithm="RS256")

    vault_dir = tmp_path / "v"
    vault_dir.mkdir()
    cfg = ServerConfig(vaults={"v": VaultConfig(name="v", path=vault_dir)})
    app = BearerAuth(build_server(cfg).streamable_http_app(), token=None, cf_access=verifier)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    accept = {"Accept": "application/json, text/event-stream"}

    with TestClient(app) as http:
        assert http.get("/health").status_code == 200
        assert http.post("/mcp", json=ping, headers=accept).status_code == 401
        # The OAuth token Access hands to clients arrives as Authorization header and is not ours to check.
        assert http.post("/mcp", json=ping, headers=accept | {"Authorization": "Bearer x"}).status_code == 401
        for bad in (token(aud=["andere-app"]), token(iss="https://evil.cloudflareaccess.com"),
                    token(exp=int(time.time()) - 10), token(signing_key=other), "kein.jwt.token"):
            assert http.post("/mcp", json=ping, headers=accept | {"Cf-Access-Jwt-Assertion": bad}).status_code == 401
        ok = http.post("/mcp", json=ping, headers=accept | {"Cf-Access-Jwt-Assertion": token()})
        assert ok.status_code == 200


def test_cloudflare_access_config_needs_team_and_aud(tmp_path, monkeypatch):
    from obsidian_mcp.config import load_config

    monkeypatch.setenv("OBSIDIAN_MCP_CF_ACCESS_TEAM", "team")
    monkeypatch.delenv("OBSIDIAN_MCP_CF_ACCESS_AUD", raising=False)
    with pytest.raises(SystemExit):
        load_config(cli_vaults=[f"v={tmp_path}"])
    monkeypatch.setenv("OBSIDIAN_MCP_CF_ACCESS_AUD", "aud-tag")
    cfg = load_config(cli_vaults=[f"v={tmp_path}"])
    assert (cfg.cf_access_team, cfg.cf_access_aud) == ("team", "aud-tag")
