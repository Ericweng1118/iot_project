#!/usr/bin/env bash
# ============================================================
# scripts/test_db.sh — 隔離測試資料庫（給 SCADA 與 PME 共用）
# ============================================================
# 為什麼需要：這台 NUC 同時是正式主機，repo 的 .env 指向正式庫。migration、權限腳本
# （例如 PME 的 sql/005_pme_role.sql）、整合測試要先在這裡跑過，才上正式庫。
#
# 內容：從正式容器複製
#   - 角色（不含密碼，scada / eric 的密碼改成 TEST_DB_PASSWORD）
#   - Energy 資料庫全量（約 85 MB，讓 energy_ext 的 FDW view 在測試庫也能查）
#   - scada 資料庫：所有 schema 與非 hypertable 的資料全量（約 175 MB），
#     hypertable（sensor_readings、ems.interval_data）只複製最近 TEST_DB_DAYS 天
# 對正式庫只做 pg_dump / SELECT，不寫入。
#
# 注意：
#   - erp_srv（tds_fdw → MSSQL ERP 192.168.1.50）的 user mapping 會一起複製，
#     在測試庫查 erp_ext 仍會讀到正式 ERP（唯讀帳號）。
#   - 容器用 --rm、沒有掛 volume：down 之後資料全部消失，下次 up + load 重建。
#   - 主機記憶體有限，預設限制 512 MB、1 顆 CPU；TimescaleDB 背景排程（壓縮）會停用。
#
# 用法：
#   scripts/test_db.sh up        建立並啟動空的測試容器
#   scripts/test_db.sh load      從正式庫複製角色、結構與資料（要先 up）
#   scripts/test_db.sh env       印出連到測試庫的環境變數（覆寫 .env 用）
#   scripts/test_db.sh psql      以 postgres 身分進入測試庫 scada
#   scripts/test_db.sh down      停止並刪除測試容器（含全部資料）
#
# 可調參數（環境變數，皆有預設值）：
#   PROD_DB_CONTAINER  正式資料庫容器名稱       預設 timescaledb
#   TEST_DB_CONTAINER  測試容器名稱             預設 scada-testdb
#   TEST_DB_PORT       測試庫對本機開放的 port  預設 55432（只綁 127.0.0.1）
#   TEST_DB_DAYS       hypertable 複製最近幾天  預設 7
#   TEST_DB_MEMORY     容器記憶體上限           預設 512m
#   TEST_DB_PASSWORD   測試庫 scada / eric 密碼 預設 scada_test_only（只在本機、用完即刪）
# ============================================================
set -euo pipefail

PROD=${PROD_DB_CONTAINER:-timescaledb}
NAME=${TEST_DB_CONTAINER:-scada-testdb}
PORT=${TEST_DB_PORT:-55432}
DAYS=${TEST_DB_DAYS:-7}
MEM=${TEST_DB_MEMORY:-512m}
PW=${TEST_DB_PASSWORD:-scada_test_only}

log() { echo "[test_db] $*" >&2; }
die() { log "錯誤：$*"; exit 1; }

# 防呆：絕對不能把測試容器指到正式庫
[[ "$NAME" != "$PROD" ]] || die "TEST_DB_CONTAINER 不能和正式容器同名（$PROD）"
[[ "$PORT" != "5432" ]] || die "TEST_DB_PORT 不能是 5432（正式庫的 port）"
[[ "$DAYS" =~ ^[0-9]+$ ]] && (( DAYS >= 1 && DAYS <= 31 )) || die "TEST_DB_DAYS 須為 1~31"

# 測試庫 / 正式庫的 psql（容器內 unix socket，以 postgres 超級使用者連線）
tpsql() { docker exec -i "$NAME" psql -U postgres -X -q -v ON_ERROR_STOP=1 "$@"; }
ppsql() { docker exec -i "$PROD" psql -U postgres -X -q -v ON_ERROR_STOP=1 "$@"; }

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" == "true" ]]; }

cmd_up() {
    running && { log "$NAME 已在執行"; return; }
    local image
    image=$(docker inspect -f '{{.Config.Image}}' "$PROD") || die "找不到正式容器 $PROD"
    log "以正式庫同一個 image（$image）啟動 $NAME，127.0.0.1:$PORT，記憶體上限 $MEM"
    docker run -d --rm --name "$NAME" \
        -p "127.0.0.1:$PORT:5432" \
        --memory "$MEM" --cpus 1 \
        -e POSTGRES_PASSWORD="$PW" -e NO_TS_TUNE=true -e TZ=Asia/Taipei \
        "$image" postgres \
        -c shared_buffers=64MB -c work_mem=4MB -c maintenance_work_mem=64MB \
        -c max_connections=30 -c max_worker_processes=8 \
        -c timescaledb.max_background_workers=1 -c timescaledb.telemetry_level=off \
        -c timezone=Asia/Taipei >/dev/null
    for _ in $(seq 1 60); do
        docker exec "$NAME" pg_isready -U postgres -q 2>/dev/null && { log "已就緒"; return; }
        sleep 1
    done
    die "等待 60 秒仍未就緒，請看 docker logs $NAME"
}

cmd_load() {
    running || die "$NAME 沒有在執行，先跑 up"
    [[ -z "$(tpsql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'scada'")" ]] \
        || die "測試庫已經有 scada 資料庫；要重來請先 down 再 up"

    log "1/5 複製角色（不含密碼）"
    # postgres 角色在測試庫已存在，那一行的錯誤可忽略，所以這段不開 ON_ERROR_STOP
    docker exec "$PROD" pg_dumpall -U postgres --roles-only --no-role-passwords \
        | docker exec -i "$NAME" psql -U postgres -X -q -d postgres 2>&1 \
        | grep -v 'role "postgres" already exists' || true
    tpsql -d postgres -c "ALTER ROLE scada PASSWORD '$PW'; ALTER ROLE eric PASSWORD '$PW';"

    log "2/5 複製 Energy 資料庫（全量）"
    tpsql -d postgres -c 'CREATE DATABASE "Energy"'
    docker exec "$PROD" pg_dump -U postgres -Fc -d Energy \
        | docker exec -i "$NAME" pg_restore -U postgres -d Energy --no-tablespaces \
        || log "Energy 還原有警告（上面的訊息），通常是既有物件，可忽略"

    log "3/5 複製 scada 結構與非 hypertable 資料（hypertable 的 chunk 只建結構、不帶資料）"
    tpsql -d postgres -c 'CREATE DATABASE scada'
    tpsql -d scada -c 'CREATE EXTENSION IF NOT EXISTS timescaledb' -c 'SELECT timescaledb_pre_restore()' >/dev/null
    docker exec "$PROD" pg_dump -U postgres -Fc -d scada \
            --exclude-table-data='_timescaledb_internal._hyper_*' \
            --exclude-table-data='_timescaledb_internal.compress_hyper_*' \
        | docker exec -i "$NAME" pg_restore -U postgres -d scada --no-tablespaces \
        || log "scada 還原有警告（上面的訊息），通常是 extension / schema 已存在，可忽略"
    tpsql -d scada -c 'SELECT timescaledb_post_restore()' >/dev/null

    log "4/5 複製 hypertable 最近 $DAYS 天的資料"
    # 從正式庫查出每個 hypertable 與它的時間欄位
    while IFS='|' read -r schema table col; do
        [[ -n "$schema" ]] || continue
        log "    $schema.$table（$col 最近 $DAYS 天）"
        # </dev/null：docker exec -i 會讀 stdin，不擋掉的話會吃掉 while 迴圈剩下的輸入
        ppsql -d scada -c "\\copy (SELECT * FROM \"$schema\".\"$table\" WHERE \"$col\" > now() - interval '$DAYS days') TO STDOUT" </dev/null \
            | tpsql -d scada -c "\\copy \"$schema\".\"$table\" FROM STDIN"
    done < <(ppsql -d scada -Atc "SELECT hypertable_schema, hypertable_name, column_name
                                  FROM timescaledb_information.dimensions WHERE dimension_number = 1")

    log "5/5 停用 TimescaleDB 背景排程（壓縮等），更新統計"
    tpsql -d scada -Atc "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs WHERE job_id >= 1000" >/dev/null
    tpsql -d scada -c 'ANALYZE' >/dev/null

    log "完成："
    tpsql -d scada -Atc "SELECT '    sensor_readings ' || count(*) || ' 筆，' || coalesce(min(reading_time)::text, '-') || ' ~ ' || coalesce(max(reading_time)::text, '-') FROM sensor_readings"
    tpsql -d scada -Atc "SELECT '    scada 大小 ' || pg_size_pretty(pg_database_size('scada'))"
    cmd_env
}

cmd_env() {
    cat <<ENV
# 連到測試庫（load_dotenv 不會覆寫已存在的環境變數，所以 export 後 .env 的正式庫設定不會生效）
export DB_HOST=127.0.0.1 DB_PORT=$PORT DB_NAME=scada DB_USER=scada DB_PASSWORD=$PW
# tests/test_bindings.py 會自建 / 刪除 bindings_test schema，需要超級使用者
export BINDINGS_TEST_DSN="postgresql://postgres:$PW@127.0.0.1:$PORT/scada"
ENV
}

cmd_psql() { running || die "$NAME 沒有在執行"; docker exec -it "$NAME" psql -U postgres -d scada; }

cmd_down() {
    docker inspect "$NAME" >/dev/null 2>&1 || { log "$NAME 不存在"; return; }
    # rm -f 是同步的；只用 docker stop 的話，--rm 的刪除在背景進行，緊接著 up 會撞名
    docker rm -f "$NAME" >/dev/null && log "已停止並刪除 $NAME（資料一併清除）"
}

case "${1:-}" in
    up) cmd_up ;;
    load) cmd_load ;;
    env) cmd_env ;;
    psql) cmd_psql ;;
    down) cmd_down ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 1 ;;
esac
