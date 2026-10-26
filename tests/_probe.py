import sqlite3
print(sqlite3.sqlite_version)
c = sqlite3.connect(":memory:")
print(c.execute("select json_extract('{\"a\":1}', '$.a')").fetchone())
print(c.execute("select json_valid('null')").fetchone())
