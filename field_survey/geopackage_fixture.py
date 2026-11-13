import sqlite3
import struct
from pathlib import Path


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
        CREATE TABLE unknown_layer (fid INTEGER PRIMARY KEY, geom BLOB);
    """)
    spatial_references = [
        ("Undefined Cartesian", -1, "NONE", -1, "undefined", "未声明的平面坐标"),
        ("Undefined Geographic", 0, "NONE", 0, "undefined", "未声明的地理坐标"),
        ("WGS 84", 4326, "EPSG", 4326, 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]', "地理坐标"),
    ]
    with connection:
        connection.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", spatial_references)
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('points','features','points',4326)")
        connection.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES ('unknown_layer','features','unknown_layer',99999)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('points','geom','POINT',4326,0,0)")
        connection.execute("INSERT INTO gpkg_geometry_columns VALUES ('unknown_layer','geom','CUSTOM',99999,0,0)")
        connection.execute("INSERT INTO gpkg_extensions VALUES ('unknown_layer','geom','local_custom','https://synthetic.invalid/custom','read-write')")
        header = b"GP" + bytes([0, 1]) + struct.pack("<i", 4326)
        point = header + struct.pack("<BIdd", 1, 1, 120.2, 30.1)
        connection.execute("INSERT INTO points VALUES (1,?,?)", ("合成样点", point))
        connection.execute("INSERT INTO points VALUES (2,?,NULL)", ("暂缺几何",))
        connection.execute("INSERT INTO unknown_layer VALUES (1,?)", (b"GP\x00\x21synthetic-extension",))
    return connection
