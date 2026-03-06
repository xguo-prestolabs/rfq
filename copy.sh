#!/usr/bin/env bash
HOST="server-01.deribit.equinix-ld4.uk.prod"
REMOTE_DIR="~/workspace/basis_strat2/20200407/wps_py"

upload() {
  local src="$1"
  local filename
  filename=$(basename "$src")
  scp "$src" "$HOST:$REMOTE_DIR/$filename"
}

upload app.py
upload ws_client_redis.py
upload zmq_to_redis.py
upload config/ws_client_config_testnet_mongodb.json
upload config/ws_client_config_mongodb_redis.json
upload config/ws_client_config_testnet_mongodb_redis.json
