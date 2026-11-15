# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `device`、`delivery`、`layer`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `wal_snapshot`：源仍有连接并提交WAL；业务侧关注同批元数据与记录属于同一快照。
- `restart_cursor`：分批领取途中退出且源继续变化；业务侧关注不把另一快照的偏移当续接证明。
- `layer_isolation`：一层含未知几何扩展；业务侧关注明确隔离该层，保留其他自洽层。

## 领域接入契约

GeoPackage输入由geopackage_fixture.py按本地配方生成真实SQLite文件，保留WAL写连接可模拟编辑中接收。基础场景包含POINT图层（正常点、缺失几何、空几何与三种不支持扩展记录）、轨迹大表、空间参考声明冲突图层和未知扩展图层，不做坐标转换。gpkg_contents、gpkg_geometry_columns、gpkg_spatial_ref_sys及GeoPackageBinary头依据OGC GeoPackage标准。

接入侧由geopackage.py以`mode=ro`加`query_only`只读打开来源，不复制正在写入的主文件冒充完整数据库，WAL中已提交的内容同样是来源事实。同一读取事务内先识别gpkg_contents、几何列与空间参考，再按该快照领取选定图层；快照身份取自WAL盐值、帧数与主库变更计数，事务首尾各取一次文件事实，不一致则回滚重试。collection.py把包身份、快照、图层状态、原始记录与按（快照，图层）保存的进度写入本地台账；另一份快照没有可继承的偏移，进程恢复时只能重新领取。空间参考声明冲突或使用未知扩展的图层被隔离，其余自洽图层照常采集。空几何、缺失几何与不支持的扩展分别标记，坐标只取自WKB原文，不从显示范围猜测位置。导出清单说明本批记录基于哪份快照、哪些层被隔离。

可用 `python3 -c 'from field_survey.geopackage_fixture import build; connection=build("sample.gpkg"); connection.close()'` 生成样例。测试调用同一函数并保持写连接，以核对尚在WAL中的提交。生成器只创建来源样例，不进行接入解析。
