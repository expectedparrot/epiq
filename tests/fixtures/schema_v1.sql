-- Original core tables, before multi-evidence, temporal metadata and identity migrations.
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('project_id','prj_v1'), ('name','Legacy fixture'),
                        ('schema_version','1'), ('created_at','2020-01-01T00:00:00Z');
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
    attributes_json TEXT NOT NULL, created_seq INTEGER NOT NULL REFERENCES events(seq),
    UNIQUE(kind,name)
);
CREATE TABLE questions (
    question_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
    subject_kind TEXT NOT NULL, value_type TEXT NOT NULL, definition_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq), UNIQUE(name,version)
);
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, content_hash TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq), UNIQUE(url,content_hash)
);
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
    excerpt TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entities(entity_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id), value_json TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_to TEXT, tx_from TEXT NOT NULL, tx_to TEXT,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id), confidence TEXT NOT NULL,
    status TEXT NOT NULL, created_seq INTEGER NOT NULL REFERENCES events(seq),
    closed_seq INTEGER REFERENCES events(seq),
    UNIQUE(subject_id,question_id,value_json,valid_from,evidence_id)
);
CREATE TABLE research_tasks (
    task_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES entities(entity_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id), status TEXT NOT NULL,
    query TEXT NOT NULL, notes TEXT NOT NULL, created_seq INTEGER NOT NULL REFERENCES events(seq),
    closed_seq INTEGER REFERENCES events(seq)
);
