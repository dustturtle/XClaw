"""Tests for investment report web APIs and admin page."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from xclaw.channels.web import create_web_app
from xclaw.config import Settings


async def _handler(chat_id: str, message: str) -> str:
    return f"{chat_id}: {message}"


def _client(db):
    app = create_web_app(
        message_handler=_handler,
        db=db,
        settings=Settings(),
    )
    return TestClient(app)


def test_admin_page_renders(db):
    client = _client(db)
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "投资日报后台" in resp.text
    assert "/api/investment/reports" in resp.text


@pytest.mark.asyncio
async def test_investment_reports_api_lists_and_reads(db):
    chat_id = await db.get_or_create_chat("web", "api_report_user")
    report_id = await db.add_investment_report(
        chat_id=chat_id,
        report_type="daily_watchlist",
        title="2026-04-14 自选股日报",
        summary="2 只股票，1 只偏多",
        content_markdown="# report",
        symbol_count=2,
        trigger_source="manual",
    )

    client = _client(db)
    list_resp = client.get(f"/api/investment/reports?chat_id={chat_id}")
    detail_resp = client.get(f"/api/investment/reports/{report_id}")

    assert list_resp.status_code == 200
    assert list_resp.json()[0]["id"] == report_id
    assert detail_resp.status_code == 200
    assert detail_resp.json()["title"] == "2026-04-14 自选股日报"


@pytest.mark.asyncio
async def test_investment_watchlist_api_crud(db):
    chat_id = await db.get_or_create_chat("web", "api_watch_user")
    client = _client(db)

    create_resp = client.post(
        "/api/investment/watchlist",
        json={"chat_id": chat_id, "symbol": "600519", "market": "CN", "name": "贵州茅台"},
    )
    list_resp = client.get(f"/api/investment/watchlist?chat_id={chat_id}")
    delete_resp = client.delete(f"/api/investment/watchlist/600519?chat_id={chat_id}&market=CN")

    assert create_resp.status_code == 200
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["symbol"] == "600519"
    assert delete_resp.status_code == 200


@pytest.mark.asyncio
async def test_investment_manual_run_api(db):
    chat_id = await db.get_or_create_chat("web", "api_run_user")
    client = _client(db)

    fake_report = {
        "id": 1,
        "title": "2026-04-14 自选股日报",
        "summary": "1 只股票，1 只偏多",
        "content_markdown": "# report body",
        "symbol_count": 1,
    }

    with patch(
        "xclaw.channels.web.InvestmentReportService.generate_report",
        AsyncMock(return_value=fake_report),
    ):
        resp = client.post("/api/investment/reports/run", json={"chat_id": chat_id, "market": "CN"})

    assert resp.status_code == 200
    assert resp.json()["title"] == "2026-04-14 自选股日报"


@pytest.mark.asyncio
async def test_investment_tasks_api_crud(db):
    chat_id = await db.get_or_create_chat("web", "api_task_user")
    client = _client(db)

    create_resp = client.post(
        "/api/investment/tasks",
        json={"chat_id": chat_id, "description": "每日自选股日报", "cron_expression": "0 18 * * 1-5"},
    )
    list_resp = client.get(f"/api/investment/tasks?chat_id={chat_id}")
    task_id = create_resp.json()["id"]
    delete_resp = client.delete(f"/api/investment/tasks/{task_id}")

    assert create_resp.status_code == 200
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["description"] == "每日自选股日报"
    assert delete_resp.status_code == 200
