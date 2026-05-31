"""CLI 入口。对 CrawlerService 的薄封装。

用法：
    python -m src.main --bv BV1xx4y1z7EG
    python -m src.main --file bv_list.txt
    python -m src.main --bv BV123 --format csv --depth 1 --auth cookie
    python -m src.main --import-cookies [chrome|edge|firefox]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loguru import logger

from src.config import Config, ensure_env_file
from src.service import CrawlerService


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="B站评论区爬取工具 — 评论爬取模块",
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--bv", type=str, help="单个 BV 号")
    source.add_argument("--file", type=str, help="批量 BV 号文件（每行一个）")
    source.add_argument("--import-cookies", nargs="?", const="auto", metavar="BROWSER",
                        help="从浏览器导入 Cookie（chrome/edge/firefox，默认 auto）")

    p.add_argument("--format", choices=["json", "csv"], default=None, help="输出格式")
    p.add_argument("--output", type=str, default=None, help="输出目录")
    p.add_argument("--depth", type=int, default=None, help="爬取深度（1=仅一级评论）")
    p.add_argument("--delay", type=float, default=None, help="请求间隔基准（秒）")
    p.add_argument("--auth", choices=["anonymous", "cookie"], default=None, help="认证模式")
    return p.parse_args()


def resolve_bv_ids(args: argparse.Namespace) -> list[str]:
    if args.bv:
        return [args.bv]
    path = Path(args.file)
    if not path.exists():
        logger.error(f"文件不存在: {path}")
        sys.exit(1)
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_results(bv_id: str, result, output_dir: Path, fmt: str) -> None:
    """将爬取结果保存为 JSON 或 CSV 文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        output_file = output_dir / f"{bv_id}.json"
        data = [c.model_dump(mode="json") for c in result.comments]
        output_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        import csv
        output_file = output_dir / f"{bv_id}.csv"

        def flatten(comments, parent_id=None):
            rows = []
            for c in comments:
                rows.append([c.rpid, parent_id, c.uid, c.username, c.content,
                             c.publish_time.isoformat(), c.likes, c.is_deleted])
                if c.replies:
                    rows.extend(flatten(c.replies, parent_id=c.rpid))
            return rows

        rows = flatten(result.comments)
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["rpid", "parent_id", "uid", "username", "content", "publish_time", "likes", "is_deleted"])
            w.writerows(rows)

    logger.info(f"已保存: {output_file}  ({len(result.comments)} 条一级评论)")


def cli_progress(event) -> None:
    """终端进度回调：简洁的单行进度输出。"""
    logger.info(event.message)


async def main_async() -> None:
    args = parse_args()
    ensure_env_file()

    # ---- Cookie 导入模式 ----
    if args.import_cookies is not None:
        from src.cookie_importer import import_cookies, save_to_env
        try:
            cookies = import_cookies(args.import_cookies)
            save_to_env(cookies)
            logger.info("Cookie 导入成功，已写入 .env")
        except RuntimeError as e:
            logger.error(str(e))
            sys.exit(1)
        return

    bv_ids = resolve_bv_ids(args)

    # 初始化：以 .env / 环境变量为基础，CLI 参数覆盖
    config = Config.from_env()
    cli_overrides = {
        k: v for k, v in {
            "auth_mode": args.auth,
            "max_reply_depth": args.depth,
            "delay_base": args.delay,
            "output_dir": args.output,
            "output_format": args.format,
        }.items() if v is not None
    }
    if cli_overrides:
        config = Config(**{**config.__dict__, **cli_overrides})
    service = CrawlerService(config)

    # 执行
    for bv_id in bv_ids:
        logger.info(f"开始爬取: {bv_id}")
        result = await service.crawl(bv_id, on_progress=cli_progress)
        save_results(bv_id, result, config.output_path, args.format)

        if result.errors:
            logger.error(f"爬取出错: {result.errors}")
        logger.info(f"完成: {result.video_title}  共 {result.total_count} 条, 耗时 {result.crawl_time:.1f}s")


def main() -> None:
    """入口：配置 loguru 后运行异步主逻辑。"""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
        level="INFO",
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
