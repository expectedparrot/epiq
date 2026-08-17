#!/usr/bin/env bash
set -euo pipefail

database="${1:-/tmp/epiq-competitors.sqlite}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
epiq_bin="${EPIQ_BIN:-epiq}"

if [[ -e "$database" ]]; then
  echo "Refusing to replace existing database: $database" >&2
  exit 2
fi

"$epiq_bin" --db "$database" init --name "Synthetic Competitor Feature Matrix"
"$epiq_bin" --db "$database" entity Product "Atlas Research"
"$epiq_bin" --db "$database" entity Product "Beacon Insights"
"$epiq_bin" --db "$database" entity Product "Cobalt Interviewer"

"$epiq_bin" --db "$database" question api_access --for Product \
  --type 'Enum[none,limited,full]' --definition '{"label":"API access"}'
"$epiq_bin" --db "$database" question model_selection --for Product \
  --type 'Enum[fixed,vendor_selected,user_selected]' --definition '{"label":"Language-model selection"}'
"$epiq_bin" --db "$database" question starting_price --for Product --type 'Quantity[USD/month]' \
  --definition '{"label":"Starting monthly price","volatility":"dynamic","freshness_days":30}'
"$epiq_bin" --db "$database" question deployment --for Product \
  --type 'Enum[cloud,on_premises,hybrid]' --definition '{"label":"Deployment model"}'
"$epiq_bin" --db "$database" question sso_support --for Product --type Bool \
  --definition '{"label":"Supports SSO"}'

"$epiq_bin" --db "$database" --actor agent:example batch-write --input "$script_dir/writeback.json"
"$epiq_bin" --db "$database" doctor

echo "Built $database"

