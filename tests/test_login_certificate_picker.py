import os

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import main


def _build_parser_app() -> FastAPI:
    app = FastAPI()

    @app.post("/parse-login")
    async def parse_login(request: Request):
        req, staged_path = await main._parse_login_submission(request)
        certificate = None
        if staged_path:
            try:
                with open(staged_path, "rb") as uploaded_file:
                    certificate = {
                        "suffix": os.path.splitext(staged_path)[1],
                        "content": uploaded_file.read().decode("ascii"),
                    }
            finally:
                os.remove(staged_path)
        return {
            "api_key": req.api_key,
            "person_id": req.person_id,
            "cert_pass": req.cert_pass,
            "save_keys": req.save_keys,
            "certificate": certificate,
        }

    return app


def test_login_parser_keeps_legacy_json_compatibility():
    client = TestClient(_build_parser_app())

    response = client.post(
        "/parse-login",
        json={
            "api_key": "api-key",
            "person_id": "person-id",
            "cert_path": "/existing/cert.pfx",
            "cert_pass": "secret",
            "save_keys": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "api_key": "api-key",
        "person_id": "person-id",
        "cert_pass": "secret",
        "save_keys": False,
        "certificate": None,
    }


def test_login_parser_stages_browser_selected_certificate(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_CERTIFICATE_DIR", str(tmp_path))
    client = TestClient(_build_parser_app())

    response = client.post(
        "/parse-login",
        data={
            "api_key": "api-key",
            "person_id": "person-id",
            "cert_pass": "secret",
            "save_keys": "false",
        },
        files={
            "cert_file": ("broker.PFX", b"certificate-data", "application/x-pkcs12"),
        },
    )

    assert response.status_code == 200
    assert response.json()["certificate"] == {
        "suffix": ".pfx",
        "content": "certificate-data",
    }
    assert response.json()["save_keys"] is False
    assert list(tmp_path.iterdir()) == []


def test_login_parser_rejects_non_pkcs12_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_CERTIFICATE_DIR", str(tmp_path))
    client = TestClient(_build_parser_app())

    response = client.post(
        "/parse-login",
        data={"save_keys": "true"},
        files={"cert_file": ("notes.txt", b"not-a-certificate", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "憑證檔案僅支援 .pfx 或 .p12"
    assert list(tmp_path.iterdir()) == []


def test_login_parser_rejects_oversized_upload_and_removes_partial_file(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(main, "_CERTIFICATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_CERTIFICATE_MAX_BYTES", 4)
    client = TestClient(_build_parser_app())

    response = client.post(
        "/parse-login",
        data={"save_keys": "true"},
        files={
            "cert_file": ("broker.p12", b"12345", "application/x-pkcs12"),
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "憑證檔案不可超過 5 MB"
    assert list(tmp_path.iterdir()) == []
