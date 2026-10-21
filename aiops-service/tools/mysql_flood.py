import sys
import time

import pymysql

host, port, user, password = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
conns = []
for i in range(180):
    try:
        conns.append(pymysql.connect(
            host=host, port=port, user=user, password=password,
            connect_timeout=2, autocommit=True,
        ))
    except Exception as exc:
        print(f"{i}: {exc}")
print(f"holding {len(conns)} connections")
time.sleep(600)
