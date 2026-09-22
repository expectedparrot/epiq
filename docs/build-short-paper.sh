#!/usr/bin/env bash
# Build the TikZ vector figure and paper; keep intermediate TeX files temporary.
set -euo pipefail
paper_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
paper_build_dir=$(mktemp -d "${TMPDIR:-/tmp}/epiq-paper.XXXXXX")
trap 'rm -rf "$paper_build_dir"' EXIT

pdflatex -interaction=nonstopmode -halt-on-error \
  -output-directory="$paper_build_dir" \
  "$paper_dir/figures/epiq-operation.tex" > "$paper_build_dir/figure-build.log" 2>&1 || {
    cat "$paper_build_dir/figure-build.log"
    exit 1
  }
cp "$paper_build_dir/epiq-operation.pdf" "$paper_dir/figures/epiq-operation.pdf"
pandoc "$paper_dir/epiq-short-paper.md" --standalone --pdf-engine=pdflatex \
  --resource-path="$paper_dir" -o "$paper_dir/epiq-short-paper.pdf"
printf 'Built %s\n' "$paper_dir/epiq-short-paper.pdf"
