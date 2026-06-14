#!/bin/sh
set -eu

output_path="${RUNTIME_CONFIG_PATH:-/usr/share/nginx/html/runtime-config.js}"
mapbox_api_key="${MAPBOX_API_KEY:-${VITE_MAPBOX_API_KEY:-}}"

if [ -n "$mapbox_api_key" ]; then
  case "$mapbox_api_key" in
    pk.*) ;;
    *)
      echo "MAPBOX_API_KEY must be a public Mapbox token beginning with pk." >&2
      exit 1
      ;;
  esac

  case "$mapbox_api_key" in
    *[!A-Za-z0-9._-]*)
      echo "MAPBOX_API_KEY contains unsupported characters." >&2
      exit 1
      ;;
  esac
fi

{
  echo "(function () {"
  echo "  window.BUILDING_CHANGE_RUNTIME_CONFIG = {"
  echo "    VITE_FASTAPI_BACKEND_URL: window.location.origin,"
  if [ -n "$mapbox_api_key" ]; then
    printf '    MAPBOX_API_KEY: "%s",\n' "$mapbox_api_key"
  fi
  echo "  };"
  echo "})();"
} > "$output_path"
