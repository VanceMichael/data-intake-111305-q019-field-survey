import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    gpkg_collect = subcommands.add_parser("gpkg-collect", help="GeoPackage 只读一致快照采集")
    gpkg_collect.add_argument("--package", type=Path, required=True)
    gpkg_collect.add_argument("--journal", type=Path, required=True)
    gpkg_collect.add_argument("--layer", action="append")
    gpkg_collect.add_argument("--batch-size", type=int, default=500)
    gpkg_collect.add_argument("--max-batches", type=int)
    gpkg_export = subcommands.add_parser("gpkg-export", help="导出一批记录及其快照身份")
    gpkg_export.add_argument("--journal", type=Path, required=True)
    gpkg_export.add_argument("--out", type=Path)
    gpkg_export.add_argument("--snapshot-id", type=int)
    gpkg_demo = subcommands.add_parser("gpkg-demo", help="本地演练编辑并发、进程恢复与不支持几何")
    gpkg_demo.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "gpkg-collect":
        from .collection import collect_package
        result = collect_package(args.package, args.journal, layers=args.layer, batch_size=args.batch_size, max_batches=args.max_batches)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "gpkg-export":
        from .collection import export_collection
        manifest = export_collection(args.journal, args.out, args.snapshot_id)
        result = {
            "snapshot_id": manifest["snapshot"]["id"],
            "snapshot_token": manifest["snapshot"]["token"],
            "layers": len(manifest["layers"]),
            "quarantined_layers": manifest["quarantined_layers"],
            "records": len(manifest["records"]),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "gpkg-demo":
        from .showcase import run
        print(json.dumps(run(args.workdir), ensure_ascii=False, indent=2))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
