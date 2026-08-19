# Epiq workflow

Select one SQLite project, inspect its schema and context, then work through gaps and review queues
without bypassing provenance.

```bash
epiq use research.sqlite
epiq init --name "Research project"
epiq schema
epiq context
epiq next
```

Define rows and typed fields before collecting evidence. Use `refresh-plan` to generate bounded
external research tasks and `record` to atomically add a source and its supported answers. Use
claim proposals for work that requires human review.

After every material stage, run `epiq next`. Before sharing or exporting a project, run `epiq
doctor` and create a backup or project bundle.
