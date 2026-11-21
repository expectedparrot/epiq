# MIT Sloan TIES candidate evidence, 2026–2027

This Epiq project records comparable, source-linked facts from the five supplied application PDFs. Repeated facts are modeled as linked rows: Education, ResearchArea, Reference, and RecommendationLetter, with each letter linked to a Candidate and Recommender. It is a documentary aid for human review, not a hiring recommendation or candidate ranking. Letter evaluations are attributed summaries of what each writer says; the writers use different language and standards, so they are not standardized scores.

## Candidates and packet counts

| Candidate | Doctoral degree/status in packet | Letters in supplied PDF | Notes |
| --- | --- | ---: | --- |
| Jennifer Walsh | Harvard Business Economics PhD, expected 2027 | 0 | Application says letters will be provided separately; none are in this packet. Five references are named in the CV. |
| Nancy Wang | MIT Economics PhD, expected 2027 | 3 | Application says three letters will be submitted separately; Autor, Duflo, and Schilbach letters appear in the packet. Application says May 2027; CV says June 2027. |
| Matthias Weigand | Harvard Economics PhD, expected 2027 | 3 | Glaeser, Shapiro, and Nunn letters appear on PDF pp.317–327. CV lists five references; that is not the same as five attached letters. |
| Zachary Wojtowicz | Carnegie Mellon PhD in Behavioral Economics, 2021 | 5 | Letters from Özdaglar, Acemoglu, Mullainathan, Andreas, and Loewenstein appear on pp.159–175. |
| Whitney Zhang | MIT Economics PhD, expected June 2027 | 3 | Acemoglu, Autor, and Danielle Li letters appear. A letter to Zachary Wojtowicz appears at p.157 and is excluded as stray packet material. |

The Candidate table holds one-row facts such as current position and the `URL_IMAGE` field. `URL_IMAGE` now contains direct HTTPS portrait URLs from profile pages, and the HTML renders thumbnails. The database also has linked doctoral/prior education, stated research areas, named references, and individual attached letters with writer-attributed evaluations. Packet letter counts can be read from RecommendationLetter rows. It omits contact information and protected personal characteristics. It does not attempt to score research quality or compare letter strength numerically.

## Use the database

From the repository root:

```bash
epiq --db epiq/examples/cli/ties-recruiting-2026-2027/candidates.sqlite matrix --kind Candidate
epiq --db epiq/examples/cli/ties-recruiting-2026-2027/candidates.sqlite dossier "Nancy Wang"
epiq --db epiq/examples/cli/ties-recruiting-2026-2027/candidates.sqlite export-html --output epiq/examples/cli/ties-recruiting-2026-2027/candidates.html
```

Each claim links to packet evidence and a PDF page locator. The original PDFs remain in the workspace root; keep them alongside this database for review. The SQLite database is ignored by Git. Treat both the PDFs and database as confidential applicant records and do not commit or distribute them casually.

## Source and review notes

- The PDFs were supplied in the workspace on October 27, 2026. Extraction used local `pdftotext`; no web sources were used.
- PDF page numbers in the evidence locators are the viewer page numbers, not printed page numbers.
- Candidate statements, CV data, and recommender statements are separate evidence types conceptually, but currently share the packet as their source document.
- Letter evaluations summarize the writer's claims and cite the page where the letter begins. Read the full letter before relying on a summary.
- Packet counts are counts of recommendation letters physically included in each supplied PDF, not the number expected, requested, or listed on a CV.
- The six earlier many-valued Candidate fields were retired after normalization. Their claim history remains in Epiq; active comparisons now use linked tables.
- Missing information means only that it was not established from the supplied packet. It is not a negative assessment.
- The first extraction pass miscounted Weigand's references as included letters. A retraction and corrected count are recorded in the Epiq event history; page review identifies three attached letters.
