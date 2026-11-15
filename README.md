# 巡测 GeoPackage 的只读一致快照采集

这是资料接收程序的基础版。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。当前不生成面向业务的有效版本，也不自动执行门户提交；重复观察会分别保留，原始内容按摘要复用。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q field_survey tests
python3 -m field_survey demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m field_survey serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m field_survey capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## GeoPackage 只读一致快照采集

巡测工作台直接交付 `.gpkg` 时，采集端以 SQLite 只读模式（`mode=ro` 加 `query_only`）打开来源文件：不复制正在写入的主文件冒充完整数据库，WAL 中已提交的内容同样是来源事实。一次接收先在同一个读取事务内识别 `gpkg_contents`、`gpkg_geometry_columns` 与 `gpkg_spatial_ref_sys`，再按该快照分批领取选定图层；元数据与记录始终来自同一份快照，不会出现"主表是新的、几何列说明是旧的"这类跨快照拼接。采集端不写回、不修改源库。

```sh
python3 -m field_survey gpkg-demo                 # 本地演练：编辑并发、进程恢复、不支持几何
python3 -m field_survey gpkg-collect --package survey.gpkg --journal .runtime/collection.sqlite3 --batch-size 500
python3 -m field_survey gpkg-collect --package survey.gpkg --journal .runtime/collection.sqlite3 --layer points
python3 -m field_survey gpkg-export --journal .runtime/collection.sqlite3 --out export.json
```

`geopackage.py` 负责只读快照接入与 GeoPackageBinary/WKB 解析；`collection.py` 是采集端自己的本地台账小库，记录保留包身份、图层、主键与原始几何，进度按（快照，图层）保存——进程退出后继续时，另一份快照没有可沿用的偏移，图层重新领取。空间参考声明冲突或使用未知扩展的图层被隔离，其余自洽图层照常采集。空几何、缺失几何与不支持的扩展分别标记，坐标只取自 WKB 原文，不从显示范围猜测位置，不做坐标变换。导出清单说明本批记录基于哪份快照、哪些层被隔离。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径；基础版测试验证的是原始采集和协议，不代表业务处理已经具备这些能力。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `field_survey` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m field_survey demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
