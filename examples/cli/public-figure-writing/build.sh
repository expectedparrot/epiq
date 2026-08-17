#!/usr/bin/env bash
set -euo pipefail

database="${1:-/tmp/epiq-public-writing.sqlite}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
epiq_bin="${EPIQ_BIN:-epiq}"

if [[ -e "$database" ]]; then
  echo "Refusing to replace existing database: $database" >&2
  exit 2
fi

"$epiq_bin" --db "$database" init --name "Public Figure and Writing"
"$epiq_bin" --db "$database" entity Person "Paul Graham"
"$epiq_bin" --db "$database" entity Institution "Cornell University"
"$epiq_bin" --db "$database" entity Institution "Harvard University"
"$epiq_bin" --db "$database" entity Publication "paulgraham.com"
"$epiq_bin" --db "$database" entity Work "Hackers and Painters"
"$epiq_bin" --db "$database" entity Work "How to Start a Startup"
"$epiq_bin" --db "$database" entity Work "Maker's Schedule, Manager's Schedule"

"$epiq_bin" --db "$database" question birth_date --for Person --type String \
  --definition '{"label":"Birth date"}'
"$epiq_bin" --db "$database" question education --for Person --type 'Ref[Institution]' \
  --definition '{"label":"Education","cardinality":"many"}'
"$epiq_bin" --db "$database" question occupation --for Person --type String \
  --definition '{"label":"Occupation","cardinality":"many"}'
"$epiq_bin" --db "$database" question author --for Work --type 'Ref[Person]' \
  --definition '{"label":"Author"}'
"$epiq_bin" --db "$database" question work_type --for Work \
  --type 'Enum[book,essay,article,paper,newsletter,blog_post]' \
  --definition '{"label":"Work type"}'
"$epiq_bin" --db "$database" question published_date --for Work --type String \
  --definition '{"label":"Publication date"}'
"$epiq_bin" --db "$database" question publication --for Work --type 'Ref[Publication]' \
  --definition '{"label":"Publication"}'
"$epiq_bin" --db "$database" question topic --for Work --type String \
  --definition '{"label":"Topic","cardinality":"many"}'
"$epiq_bin" --db "$database" question canonical_url --for Work --type String \
  --definition '{"label":"Canonical URL"}'

"$epiq_bin" --db "$database" --actor agent:example batch-write --input "$script_dir/writeback.json"
"$epiq_bin" --db "$database" doctor

echo "Built $database"

