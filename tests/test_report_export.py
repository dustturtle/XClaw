"""Tests for report PDF/image export generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from xclaw.config import Settings
from xclaw.investment.report_export_service import ReportExportService


@pytest.mark.asyncio
async def test_report_export_service_generates_pdf_and_images(db, tmp_path: Path):
    chat_id = await db.get_or_create_chat("web", "pdf_user")
    report_id = await db.add_investment_report(
        chat_id=chat_id,
        report_type="daily_watchlist",
        title="2026-04-14 自选股日报",
        summary="2 只股票，1 只偏多，1 只观察",
        content_markdown=(
            "# 2026-04-14 自选股日报\n\n"
            "摘要：2 只股票，1 只偏多，1 只观察\n\n"
            "## 市场概览\n主要指数：\n- 上证指数: 3200 (+0.8%)\n\n"
            "## 个股策略卡\n"
            "### 贵州茅台 600519\n"
            "高价值策略数：2\n"
            "- bull_trend | triggered | 买入区 10.10-10.20 | 止损 9.80 | 目标 10.80\n"
            "  条件：回踩 MA5 企稳\n\n"
            "### 平安银行 000001\n"
            "高价值策略数：0\n"
            "- box_oscillation | watch | 买入区 9.10-9.20 | 止损 8.90 | 目标 9.80\n"
            "  条件：箱底附近观察\n"
        ),
        symbol_count=2,
        trigger_source="manual",
    )

    service = ReportExportService(
        db=db,
        settings=Settings(data_dir=str(tmp_path / "data")),
    )

    assets = await service.generate_assets(report_id)

    assert assets["pdf"]["mime_type"] == "application/pdf"
    assert Path(assets["pdf"]["file_path"]).exists()
    assert len(assets["images"]) >= 2
    for image in assets["images"]:
        assert image["mime_type"] == "image/png"
        assert Path(image["file_path"]).exists()
