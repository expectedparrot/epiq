#!/usr/bin/env bash
set -euo pipefail
database="${1:-/tmp/epiq-literature.sqlite}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
epiq_bin="${EPIQ_BIN:-epiq}"
"$epiq_bin" --db "$database" --quiet apply --input "$script_dir/schema.json"
"$epiq_bin" --db "$database" --actor agent:example --quiet batch-write --input "$script_dir/writeback.json"
"$epiq_bin" --db "$database" --select ok doctor
echo "Built $database"
