#!/bin/sh
set -eu

upstream="${GATEWAY_UPSTREAM:-}"
host="${upstream%:*}"
port="${upstream##*:}"

case "$upstream" in
  ""|*[!A-Za-z0-9._:-]*|"$host")
    echo "GATEWAY_UPSTREAM must be a host:port value" >&2
    exit 1
    ;;
esac

case "$host:$port" in
  :*|*:|*:*[!0-9]*)
    echo "GATEWAY_UPSTREAM must be a host with a numeric port" >&2
    exit 1
    ;;
esac
