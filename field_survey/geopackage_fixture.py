import math
import sqlite3
import struct
from pathlib import Path

TRACK_POINT_ROWS = 250


def gpkg_header(flags, srs_id, padding=b""):
    return b"GP" + bytes([0, flags]) + struct.pack("<i", srs_id) + padding


def point_geometry(x, y):
    return gpkg_header(1, 4326) + struct.pack("<BIdd", 1, 1, x, y)


def empty_point_geometry():
    return gpkg_header(0x11, 4326) + struct.pack("<BIdd", 1, 1, math.nan, math.nan)


def circularstring_geometry():
    body = struct.pack("<BI", 1, 8) + struct.pack("<I", 3)
    for x, y in ((120.3, 30.2), (120.4, 30.3), (120.5, 30.2)):
        body += struct.pack("<dd", x, y)
    return gpkg_header(1, 4326) + body


def reserved_envelope_geometry():
    return gpkg_header(0x0B, 4326, b"\x00" * 16) + struct.pack("<BIdd", 1, 1, 120.6, 30.4)


def ewkb_geometry():
    return gpkg_header(1, 4326) + struct.pack("<BIi", 1, 0x20000001, 4326) + struct.pack("<dd", 120.7, 30.5)


def build(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("PRAGMA application_id=1196444487")
    connection.execute("PRAGMA user_version=10300")
    connection.executescript("""
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY,
            organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL, description TEXT
        );
        CREATE TABLE gpkg_contents (
            table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL,
            identifier TEXT UNIQUE, description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT '2026-11-01T00:00:00.000Z',
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER
        );
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL, column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL, m TINYINT NOT NULL,
            PRIMARY KEY(table_name, column_name)
        );
        CREATE TABLE gpkg_extensions (
            table_name TEXT, column_name TEXT, extension_name TEXT NOT NULL,
            definition TEXT NOT NULL, scope TEXT NOT NULL
        );
        CREATE TABLE points (fid INTEGER PRIMARY KEY, name TEXT, geom BLOB);
        CREATE TABLE track_points (fid INTEGER PRIMARY KEY, seq INTEGER, label TEXT, geom BLOB);
        CREATE TABLE conflicted_layer (fid INTEGER PRIMARY KEY, geom BLOB);
        CREATE TABLE unknown_layer (fid INTEGER PRIMARY KEY, geom BLOB);
    """)
    spatial_references = [
        ("Undefined Cartesian", -1, "NONE", -1, "undefined", "未声明的平面坐标"),
        ("Undefined Geographic", 0, "NONE", 0, "undefined", "未声明的地理坐标"),
        ("WGS 84", 4326, "EPSG", 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', "地理坐标"),
        ("WGS 84 / Pseudo-Mercator", 3857, "EPSG", 3857, 'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Mercator_1SP"],UNIT["metre",1]]', "投影坐标"),
    ]
    with connection:
        connection.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", spatial_references)
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id,min_x,min_y,max_x,max_y) VALUES ('points','features','points',4326,120.0,30.0,121.0,31.0)")
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('track_points','features','track_points',4326)")
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('conflicted_layer','features','conflicted_layer',4326)")
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('unknown_layer','features','unknown_layer',99999)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('points','geom','POINT',4326,0,0)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('track_points','geom','POINT',4326,0,0)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('conflicted_layer','geom','POINT',3857,0,0)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('unknown_layer','geom','CUSTOM',99999,0,0)")
        connection.execute("INSERT INTO gpkg_extensions VALUES ('unknown_layer','geom','local_custom','https://synthetic.invalid/custom','read-write')")
        connection.execute("INSERT INTO points VALUES (1,?,?)", ("合成样点", point_geometry(120.2, 30.1)))
        connection.execute("INSERT INTO points VALUES (2,?,NULL)", ("暂缺几何",))
        connection.execute("INSERT INTO points VALUES (3,?,?)", ("空点", empty_point_geometry()))
        connection.execute("INSERT INTO points VALUES (4,?,?)", ("曲线扩展", circularstring_geometry()))
        connection.execute("INSERT INTO points VALUES (5,?,?)", ("保留包络指示", reserved_envelope_geometry()))
        connection.execute("INSERT INTO points VALUES (6,?,?)", ("EWKB身份标记", ewkb_geometry()))
        connection.execute("INSERT INTO conflicted_layer VALUES (1,?)", (point_geometry(119.0, 29.0),))
        connection.execute("INSERT INTO unknown_layer VALUES (1,?)", (b"GP\x00\x21synthetic-extension",))
        connection.executemany(
            "INSERT INTO track_points VALUES (?,?,?,?)",
            [
                (seq, seq, "轨迹点-%d" % seq, point_geometry(120.0 + seq * 0.001, 30.0 + seq * 0.001))
                for seq in range(1, TRACK_POINT_ROWS + 1)
            ],
        )
    return connection
