#!/usr/bin/env python3
"""
Codex to Kavita Migration Script

This script migrates reading progress from a Codex reading server database
(codex.sqlite, a Django/SQLite database) to a Kavita database.

Codex records progress in `codex_bookmark`: one row per (user, comic) holding a
0-indexed `page` and a `finished` flag. Unlike ComicRack, Codex stores the real
file path of every comic, and so does Kavita, so this script matches on paths
rather than on fuzzy series names:

- Each database declares its own library roots (`codex_library.path` and Kavita's
  `FolderPath.Path`). Those roots are stripped from both sides, so the two servers
  do not need to mount the library at the same place - a Codex "/comics" and a
  Kavita "/data/media/comics" holding the same tree still line up.
- The remaining relative directory identifies the run exactly. If the trees are
  rooted differently than the declared roots suggest, the longest unique trailing
  path match is used instead.
- Inside that directory the issue is resolved on Codex's `issue_number` against
  Kavita's parsed `Chapter.MinNumber`, with page count as the tie-break.

Because the path anchor is exact, there is no run-scoring or arbitration phase:
anything that fails to match is a book the target library genuinely does not have,
and it is reported rather than guessed at.

Usage:
    python Codex_Migration.py --codex-db /path/to/codex.sqlite --kavita-db /path/to/kavita.db --username "YourKavitaUsername" [options]

Requirements:
    - Python 3.7+
    - No external dependencies
"""

import sqlite3
import argparse
import sys
import os
import collections
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any, Set

# Codex page numbers are 0-indexed, Kavita's PagesRead is a count of pages finished.
# A comic open on page N has therefore completed N pages.
PAGE_INDEX_OFFSET = 0


class CodexMigrator:
    """Handles migration of reading progress from a Codex database to Kavita."""

    def __init__(self, codex_db_path: str, kavita_db_path: str, username: str,
                 codex_user: Optional[str] = None, dry_run: bool = False,
                 verbose: bool = False, report_path: Optional[str] = None,
                 path_map: Optional[List[Tuple[str, str]]] = None):
        """
        Initialize the Codex migrator.

        Args:
            codex_db_path: Path to the Codex SQLite database (typically codex.sqlite)
            kavita_db_path: Path to Kavita SQLite database
            username: Kavita username to migrate progress to
            codex_user: Codex username whose bookmarks to read; defaults to the only
                        user with progress, or errors if there is more than one
            dry_run: If True, don't make any changes to the Kavita database
            verbose: If True, print a line for every bookmark instead of only the summary
            report_path: Optional file to write the full skip breakdown to
            path_map: Optional (old_prefix, new_prefix) rewrites applied to Codex paths
                      before root stripping, for libraries that were also reorganised
        """
        self.codex_db_path = codex_db_path
        self.kavita_db_path = kavita_db_path
        self.username = username
        self.codex_user = codex_user
        self.dry_run = dry_run
        self.verbose = verbose
        self.report_path = report_path
        self.path_map = path_map or []

        self.codex_conn: Optional[sqlite3.Connection] = None
        self.kavita_conn: Optional[sqlite3.Connection] = None
        self.user_id: Any = None
        self.codex_user_id: Any = None
        self.codex_user_label: str = ''

        # Kavita library roots, longest first so nested roots resolve correctly
        self.kavita_roots: List[str] = []
        # Codex library id -> declared root path
        self.codex_roots: Dict[int, str] = {}

        # relative directory -> list of candidate chapter dicts
        self.dir_index: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        # trailing path fragment -> set of relative directories holding it
        self.suffix_index: Dict[str, Set[str]] = collections.defaultdict(set)

        # Reasons bookmarks were skipped, for the end-of-run report
        self.unmatched_folder: collections.Counter = collections.Counter()
        self.unmatched_issue: collections.Counter = collections.Counter()
        self.ambiguous: collections.Counter = collections.Counter()
        # Chapters a dry run would have written, so previews match a real run exactly
        self._pending: Dict[int, int] = {}
        self.codex_bookmark_count = 0
        self.kavita_chapter_count = 0
        self.kavita_dir_count = 0
        self.page_count_mismatches = 0
        self.unanalyzed = 0

    # ------------------------------------------------------------- connections

    def connect(self):
        """Connect to both databases, validate them, and resolve both usernames."""
        if not os.path.exists(self.codex_db_path):
            raise FileNotFoundError(f"Codex database not found: {self.codex_db_path}")

        if not os.path.exists(self.kavita_db_path):
            raise FileNotFoundError(f"Kavita database not found: {self.kavita_db_path}")

        self.codex_conn = sqlite3.connect(f"file:{self.codex_db_path}?mode=ro", uri=True)
        self.codex_conn.row_factory = sqlite3.Row

        cursor = self.codex_conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM codex_bookmark")
        except sqlite3.DatabaseError as e:
            raise ValueError(f"{self.codex_db_path} does not look like a Codex database: {e}")

        self.kavita_conn = sqlite3.connect(self.kavita_db_path)
        self.kavita_conn.row_factory = sqlite3.Row

        # Find the Kavita user to write progress for
        cursor = self.kavita_conn.cursor()
        cursor.execute("SELECT Id FROM AspNetUsers WHERE UserName = ?", (self.username,))
        user_row = cursor.fetchone()

        if not user_row:
            cursor.execute("SELECT UserName FROM AspNetUsers ORDER BY UserName")
            known = ', '.join(r['UserName'] for r in cursor.fetchall())
            raise ValueError(f"User '{self.username}' not found in Kavita database. Known users: {known}")

        self.user_id = user_row['Id']
        self._resolve_codex_user()

    def _resolve_codex_user(self):
        """
        Pick which Codex user's bookmarks to migrate.

        Codex is multi-user and also keeps anonymous, session-scoped bookmarks. Guessing
        between two real users would silently import a stranger's history, so a database
        with progress for more than one user has to be told which one to read.
        """
        assert self.codex_conn is not None, "Not connected to Codex database"
        cursor = self.codex_conn.cursor()

        if self.codex_user is not None:
            cursor.execute("SELECT id, username FROM auth_user WHERE username = ?", (self.codex_user,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT username FROM auth_user ORDER BY username")
                known = ', '.join(r['username'] for r in cursor.fetchall())
                raise ValueError(f"Codex user '{self.codex_user}' not found. Known users: {known}")
            self.codex_user_id = row['id']
            self.codex_user_label = row['username']
            return

        cursor.execute("""
            SELECT b.user_id, u.username, COUNT(*) AS n
            FROM codex_bookmark b
            LEFT JOIN auth_user u ON u.id = b.user_id
            WHERE b.user_id IS NOT NULL
            GROUP BY b.user_id
            ORDER BY n DESC
        """)
        rows = cursor.fetchall()

        if not rows:
            raise ValueError("No Codex bookmarks belong to a registered user "
                             "(only anonymous session bookmarks were found).")
        if len(rows) > 1:
            listing = ', '.join(f"{r['username']} ({r['n']} bookmarks)" for r in rows)
            raise ValueError(f"Codex database holds progress for {len(rows)} users. "
                             f"Pass --codex-user to choose one: {listing}")

        self.codex_user_id = rows[0]['user_id']
        self.codex_user_label = rows[0]['username'] or f"id={rows[0]['user_id']}"

    def disconnect(self):
        """Disconnect from both databases, committing Kavita changes on a real run."""
        if self.kavita_conn:
            if not self.dry_run:
                self.kavita_conn.commit()
            self.kavita_conn.close()
            self.kavita_conn = None
        if self.codex_conn:
            self.codex_conn.close()
            self.codex_conn = None

    # ------------------------------------------------------------------- paths

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize separators and drop any trailing slash, without touching case."""
        return path.replace('\\', '/').rstrip('/')

    def _apply_path_map(self, path: str) -> str:
        """Apply user-supplied prefix rewrites, longest prefix first."""
        for old, new in self.path_map:
            if path == old or path.startswith(old + '/'):
                return new + path[len(old):]
        return path

    @staticmethod
    def _strip_root(path: str, roots: List[str]) -> Optional[str]:
        """
        Remove the longest declared library root from a path.

        Returns:
            The path relative to that root, or None if no root contains it
        """
        best: Optional[str] = None
        for root in roots:
            if path == root or path.startswith(root + '/'):
                if best is None or len(root) > len(best):
                    best = root
        if best is None:
            return None
        return path[len(best):].lstrip('/')

    @staticmethod
    def _suffixes(relative_dir: str) -> List[str]:
        """Every trailing fragment of a relative directory, longest first."""
        parts = [p for p in relative_dir.split('/') if p]
        return ['/'.join(parts[i:]) for i in range(len(parts))]

    # ----------------------------------------------------------------- indexes

    def build_kavita_index(self):
        """
        Index every Kavita chapter by the directory its files live in, relative to
        the library root that holds it.

        A chapter can own several files (a .cbr and a .cbz of the same issue), so the
        index is keyed on chapter and records the first file seen for tie-breaking.
        """
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()

        cursor.execute("SELECT Path FROM FolderPath")
        self.kavita_roots = sorted(
            (self._normalize_path(r['Path']) for r in cursor.fetchall() if r['Path']),
            key=len, reverse=True
        )
        if not self.kavita_roots:
            raise ValueError("Kavita database declares no library folders (FolderPath is empty).")

        cursor.execute("""
            SELECT m.FilePath, m.Pages AS FilePages, c.Id AS ChapterId, c.Pages AS ChapterPages,
                   c.MinNumber, c.MaxNumber, v.Id AS VolumeId, s.Id AS SeriesId,
                   s.Name AS SeriesName, s.LibraryId
            FROM MangaFile m
            JOIN Chapter c ON c.Id = m.ChapterId
            JOIN Volume v ON v.Id = c.VolumeId
            JOIN Series s ON s.Id = v.SeriesId
            WHERE m.FilePath IS NOT NULL
        """)

        seen: Set[Tuple[str, int]] = set()
        for row in cursor:
            relative = self._strip_root(self._normalize_path(row['FilePath']), self.kavita_roots)
            if relative is None:
                continue
            relative_dir = os.path.dirname(relative)
            key = (relative_dir, row['ChapterId'])
            if key in seen:
                continue
            seen.add(key)
            self.dir_index[relative_dir].append({
                'chapter_id': row['ChapterId'],
                'volume_id': row['VolumeId'],
                'series_id': row['SeriesId'],
                'series_name': row['SeriesName'],
                'library_id': row['LibraryId'],
                'chapter_pages': row['ChapterPages'],
                'file_pages': row['FilePages'],
                'min_number': row['MinNumber'],
                'max_number': row['MaxNumber'],
                'file_name': os.path.basename(relative),
            })

        for relative_dir in self.dir_index:
            for suffix in self._suffixes(relative_dir):
                self.suffix_index[suffix].add(relative_dir)

        self.kavita_dir_count = len(self.dir_index)
        self.kavita_chapter_count = len(seen)

    # ------------------------------------------------------------ codex source

    def load_bookmarks(self) -> List[Dict[str, Any]]:
        """
        Read the chosen user's bookmarks, keeping only those that carry real progress.

        Returns:
            A list of dicts describing each readable bookmark
        """
        assert self.codex_conn is not None, "Not connected to Codex database"
        cursor = self.codex_conn.cursor()

        cursor.execute("SELECT id, path FROM codex_library")
        self.codex_roots = {r['id']: self._normalize_path(r['path']) for r in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) AS n FROM codex_bookmark WHERE user_id = ?",
                       (self.codex_user_id,))
        self.codex_bookmark_count = cursor.fetchone()['n']

        cursor.execute("""
            SELECT b.page, b.finished, b.created_at, b.updated_at,
                   c.path, c.page_count, c.issue_number, c.library_id,
                   s.name AS series_name
            FROM codex_bookmark b
            JOIN codex_comic c ON c.id = b.comic_id
            JOIN codex_series s ON s.id = c.series_id
            WHERE b.user_id = ?
        """, (self.codex_user_id,))

        bookmarks: List[Dict[str, Any]] = []
        for row in cursor:
            bookmarks.append({
                'page': row['page'],
                'finished': bool(row['finished']),
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
                'path': row['path'],
                'page_count': row['page_count'],
                'issue_number': row['issue_number'],
                'library_id': row['library_id'],
                'series_name': row['series_name'] or '(no series)',
            })
        return bookmarks

    # ---------------------------------------------------------------- matching

    def resolve_directory(self, relative_dir: str) -> Optional[List[Dict[str, Any]]]:
        """
        Find the Kavita directory holding a Codex relative directory.

        Falls back to the longest trailing fragment that identifies exactly one Kavita
        directory, so a library rooted differently than its declared root still matches.
        An ambiguous fragment is treated as no match rather than picking one.

        Returns:
            The candidate chapters in that directory, or None
        """
        exact = self.dir_index.get(relative_dir)
        if exact:
            return exact

        for suffix in self._suffixes(relative_dir):
            matches = self.suffix_index.get(suffix)
            if matches and len(matches) == 1:
                return self.dir_index[next(iter(matches))]
        return None

    def resolve_chapter(self, item: Dict[str, Any],
                        candidates: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Pick the Kavita chapter matching one Codex comic inside an already-matched directory.

        Returns:
            (chapter, reason) where chapter is None on failure and reason is 'ok',
            'no_issue' or 'ambiguous'
        """
        issue_number = item['issue_number']
        page_count = item['page_count']

        hits: List[Dict[str, Any]] = []
        if issue_number is not None:
            target = float(issue_number)
            hits = [c for c in candidates
                    if c['min_number'] is not None and abs(c['min_number'] - target) < 1e-6]

        if len(hits) == 1:
            return hits[0], 'ok'

        if len(hits) > 1:
            # Usually the same issue stored twice (a .cbr and a .cbz, or a "(1)" copy).
            # Page count separates a genuine duplicate from a different book.
            narrowed = [c for c in hits if c['file_pages'] == page_count]
            if len(narrowed) == 1:
                return narrowed[0], 'ok'
            # Same chapter reached through two files is not a real ambiguity
            if len({c['chapter_id'] for c in hits}) == 1:
                return hits[0], 'ok'
            return None, 'ambiguous'

        # No issue number, or Kavita parsed this run's numbering differently: a unique
        # page count inside the directory is still solid evidence.
        by_pages = [c for c in candidates if c['file_pages'] == page_count]
        if len(by_pages) == 1:
            return by_pages[0], 'ok'
        return None, 'no_issue'

    # ------------------------------------------------------------------- dates

    @staticmethod
    def _format_kavita_datetime(dt: datetime) -> str:
        """Format a datetime for Kavita's SQLite fields without relying on sqlite3's datetime."""
        if dt.microsecond:
            # Trim trailing zeros to avoid overly long strings
            return dt.strftime('%Y-%m-%d %H:%M:%S.%f').rstrip('0').rstrip('.')
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    @staticmethod
    def parse_codex_datetime(value: Any) -> Optional[datetime]:
        """
        Parse a Django datetime string from Codex.

        Django stores these in UTC as "2026-05-10 09:45:33.531551", occasionally with a
        "T" separator or a trailing "+00:00".

        Returns:
            A naive UTC datetime, or None if parsing fails
        """
        if not value:
            return None
        text = str(value).strip().replace('T', ' ')
        if text.endswith('+00:00'):
            text = text[:-6]
        elif text.endswith('Z'):
            text = text[:-1]
        # Django may write more precision than %f accepts
        if '.' in text:
            head, _, frac = text.partition('.')
            text = f"{head}.{frac[:6]}"
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _timestamps(self, item: Dict[str, Any]) -> Dict[str, str]:
        """
        Build Kavita's four timestamp fields from a Codex bookmark.

        Codex timestamps are UTC. Kavita keeps UTC in its *Utc columns and server-local
        time in the others, so the local pair is converted rather than copied.
        """
        created = self.parse_codex_datetime(item['created_at'])
        modified = self.parse_codex_datetime(item['updated_at']) or created
        if created is None:
            created = modified = datetime.now(timezone.utc).replace(tzinfo=None)
        if modified is None:
            modified = created

        def to_local(dt: datetime) -> datetime:
            return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

        return {
            'created_utc': self._format_kavita_datetime(created),
            'created_local': self._format_kavita_datetime(to_local(created)),
            'modified_utc': self._format_kavita_datetime(modified),
            'modified_local': self._format_kavita_datetime(to_local(modified)),
        }

    # --------------------------------------------------------------- migration

    def migrate_progress(self):
        """Migrate reading progress from Codex to Kavita."""
        mode = "DRY RUN (no changes written)" if self.dry_run else "APPLYING CHANGES"
        print(f"Codex -> Kavita  |  {self.codex_user_label} -> {self.username}  |  {mode}")
        print(f"  source: {self.codex_db_path}")
        print(f"  target: {self.kavita_db_path}\n")

        bookmarks = self.load_bookmarks()
        if not bookmarks:
            print("No bookmarks found for this Codex user")
            return

        self.build_kavita_index()

        stats: collections.Counter = collections.Counter()
        errors: List[str] = []
        claims: List[Dict[str, Any]] = []

        for item in bookmarks:
            try:
                label = os.path.basename(item['path'])

                relative = self._strip_root(
                    self._apply_path_map(self._normalize_path(item['path'])),
                    [self.codex_roots[item['library_id']]] if item['library_id'] in self.codex_roots else []
                )
                if relative is None:
                    stats['bad_data'] += 1
                    continue

                candidates = self.resolve_directory(os.path.dirname(relative))
                if not candidates:
                    stats['no_folder'] += 1
                    self.unmatched_folder[item['series_name']] += 1
                    if self.verbose:
                        print(f"  - {label} (folder not in Kavita)")
                    continue

                chapter, reason = self.resolve_chapter(item, candidates)
                if chapter is None:
                    if reason == 'ambiguous':
                        stats['ambiguous'] += 1
                        self.ambiguous[item['series_name']] += 1
                    else:
                        stats['no_issue'] += 1
                        self.unmatched_issue[item['series_name']] += 1
                    if self.verbose:
                        print(f"  - {label} ({'ambiguous match' if reason == 'ambiguous' else 'issue not in Kavita'})")
                    continue

                pages_read = self._pages_read(item, chapter)
                if pages_read <= 0:
                    stats['no_progress'] += 1
                    if self.verbose:
                        print(f"  = {label} (opened but never read past page 1)")
                    continue

                if chapter['file_pages'] != item['page_count']:
                    self.page_count_mismatches += 1

                item.update({'chapter': chapter, 'pages_read': pages_read, 'label': label})
                claims.append(item)
            except Exception as e:
                stats['error'] += 1
                errors.append(f"{item.get('path', '?')}: {e}")

        # Two Codex files can land on one Kavita chapter (a duplicate scan). Writing the
        # furthest-read one first keeps the merge order irrelevant.
        claims.sort(key=lambda c: c['pages_read'], reverse=True)

        for item in claims:
            try:
                stats[self._write_progress(item)] += 1
            except Exception as e:
                stats['error'] += 1
                errors.append(f"{item['path']}: {e}")

        self._print_summary(len(bookmarks), stats, errors)

    def _pages_read(self, item: Dict[str, Any], chapter: Dict[str, Any]) -> int:
        """
        Translate a Codex bookmark into a Kavita PagesRead count.

        A finished comic is written as fully read using *Kavita's* page count, since the
        two servers may hold different scans of the same issue. An unfinished one is
        clamped one page short of the end so it cannot be mistaken for a completed read.

        A chapter Kavita has not analyzed yet reports zero pages. Codex's own count is
        used there: any positive value already reads as finished to Kavita, and the row
        becomes exactly right once the library is scanned.
        """
        chapter_pages = chapter['chapter_pages'] or 0
        if not chapter_pages:
            self.unanalyzed += 1

        if item['finished']:
            return chapter_pages if chapter_pages else (item['page_count'] or 0)

        page = item['page']
        if not page or page <= 0:
            return 0
        pages_read = page + PAGE_INDEX_OFFSET
        if chapter_pages:
            pages_read = min(pages_read, chapter_pages - 1)
        return max(pages_read, 0)

    def _write_progress(self, item: Dict[str, Any]) -> str:
        """
        Write one matched bookmark to Kavita.

        Returns:
            One of: inserted, updated, current
        """
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        chapter = item['chapter']
        pages_read = item['pages_read']
        times = self._timestamps(item)
        label = f"{item['label']} -> {chapter['series_name']}"

        cursor = self.kavita_conn.cursor()
        cursor.execute("""
            SELECT Id, PagesRead FROM AppUserProgresses
            WHERE AppUserId = ? AND ChapterId = ?
        """, (self.user_id, chapter['chapter_id']))
        existing = cursor.fetchone()

        prefix = '[DRY RUN] ' if self.dry_run else ''
        existing_pages = existing['PagesRead'] if existing else None

        # A real run sees its own uncommitted writes; a dry run has to simulate them, or
        # two Codex files sharing a Kavita chapter would both look like fresh inserts.
        if self.dry_run and chapter['chapter_id'] in self._pending:
            simulated = self._pending[chapter['chapter_id']]
            existing_pages = simulated if existing_pages is None else max(existing_pages, simulated)

        if existing_pages is not None:
            if pages_read <= existing_pages:
                if self.verbose:
                    print(f"  = {prefix}{label} (Kavita already at {existing_pages} pages)")
                return 'current'
            if self.dry_run:
                self._pending[chapter['chapter_id']] = pages_read
                if self.verbose:
                    print(f"  ^ {prefix}Would update: {label} "
                          f"({existing_pages} -> {pages_read}/{chapter['chapter_pages']} pages)")
                return 'updated'
            cursor.execute("""
                UPDATE AppUserProgresses
                SET PagesRead = ?,
                    LastModified = ?,
                    LastModifiedUtc = ?
                WHERE Id = ?
            """, (pages_read, times['modified_local'], times['modified_utc'], existing['Id']))
            if self.verbose:
                print(f"  ^ Updated: {label} ({pages_read}/{chapter['chapter_pages']} pages)")
            return 'updated'

        if self.dry_run:
            self._pending[chapter['chapter_id']] = pages_read
            if self.verbose:
                print(f"  + {prefix}Would migrate: {label} "
                      f"({pages_read}/{chapter['chapter_pages']} pages)")
            return 'inserted'

        cursor.execute("""
            INSERT INTO AppUserProgresses
            (AppUserId, ChapterId, VolumeId, SeriesId, LibraryId, PagesRead,
             Created, LastModified, CreatedUtc, LastModifiedUtc, TotalReads)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (self.user_id, chapter['chapter_id'], chapter['volume_id'], chapter['series_id'],
              chapter['library_id'], pages_read,
              times['created_local'], times['modified_local'],
              times['created_utc'], times['modified_utc']))
        if self.verbose:
            print(f"  + Migrated: {label} ({pages_read}/{chapter['chapter_pages']} pages)")
        return 'inserted'

    # ----------------------------------------------------------------- reports

    def _print_summary(self, total: int, stats: collections.Counter, errors: List[str]):
        """Print a short, actionable report of what the run did."""
        matched = stats['inserted'] + stats['updated'] + stats['current']
        skipped = total - matched

        print(f"Read    {total} bookmarks for Codex user {self.codex_user_label} "
              f"({self.codex_bookmark_count} total)")
        print(f"Indexed {self.kavita_chapter_count} chapters across "
              f"{self.kavita_dir_count} folders from Kavita")

        verb = "Would match" if self.dry_run else "Matched"
        rate = f" ({matched / total:.1%})" if total else ""
        print(f"\n{verb} {matched} of {total} bookmarks{rate}")

        # "existing progress" may be a row already in Kavita or one this same run just
        # wrote for a duplicate Codex file, so the wording stays neutral about which.
        if self.dry_run:
            labels = ("new", "would raise existing progress", "already at or beyond this point")
        else:
            labels = ("written", "raised existing progress", "left as-is, already further along")
        detail = [f"{stats[k]} {label}"
                  for k, label in zip(('inserted', 'updated', 'current'), labels)
                  if stats[k]]
        if detail:
            print("        " + ", ".join(detail))

        if self.page_count_mismatches:
            print(f"\nNote    {self.page_count_mismatches} matched comics have a different page "
                  f"count in Kavita than in Codex\n"
                  f"        (different scans of the same issue; Kavita's count was used)")

        if self.unanalyzed:
            print(f"\nNote    {self.unanalyzed} matched comics have not been analyzed by Kavita "
                  f"yet (0 pages known)\n"
                  f"        Codex's page count was used; run a Kavita library scan to correct them")

        # Skip reasons, largest first, with a few example series inline
        reasons = [
            (stats['no_folder'], "not in this Kavita library", self.unmatched_folder),
            (stats['no_issue'], "issue not in Kavita", self.unmatched_issue),
            (stats['no_progress'], "no progress recorded in Codex", None),
            (stats['ambiguous'], "ambiguous match, not guessed", self.ambiguous),
            (stats['bad_data'], "path outside any Codex library root", None),
            (stats['error'], "errors", None),
        ]
        reasons = [r for r in reasons if r[0]]

        if skipped:
            print(f"\nSkipped {skipped} bookmarks")
            for count, label, counter in reasons:
                line = f"  {count:5d}  {label:<32}"
                if counter:
                    names = [n if len(n) <= 34 else n[:31] + '...'
                             for n, _ in counter.most_common(3)]
                    more = ", ..." if len(counter) > 3 else ""
                    line += f"  {len(counter)} series: {', '.join(names)}{more}"
                print(line.rstrip())

        if errors:
            print(f"\nFirst error: {errors[0]}")

        if self.report_path:
            self._write_report(stats, errors)
            print(f"\nFull breakdown: {self.report_path}")
        elif skipped:
            print("\nRun with --report FILE for the full list of skipped series.")

        if self.dry_run:
            print("\nDRY RUN - nothing was written. Re-run without --dry-run to apply.")

    def _write_report(self, stats: collections.Counter, errors: List[str]):
        """Write the full skip breakdown to a file, so the console stays short."""
        sections = [
            ("NOT IN THIS KAVITA LIBRARY",
             "No folder matching this comic's Codex folder exists in Kavita. The target\n"
             "library simply does not hold this series.",
             self.unmatched_folder),
            ("ISSUE NOT IN KAVITA",
             "The series folder exists in Kavita, but not this issue number.",
             self.unmatched_issue),
            ("AMBIGUOUS MATCH - NOT GUESSED",
             "Several different Kavita chapters in the folder carry this issue number and\n"
             "page count could not separate them. Skipped rather than written to a possibly\n"
             "wrong issue.",
             self.ambiguous),
        ]
        with open(self.report_path, 'w', encoding='utf-8') as fh:
            fh.write("Codex -> Kavita migration report\n")
            fh.write(f"Generated:  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"Codex user: {self.codex_user_label}\n")
            fh.write(f"User:       {self.username}\n")
            fh.write(f"Source:     {self.codex_db_path}\n")
            fh.write(f"Target:     {self.kavita_db_path}\n")
            fh.write(f"Mode:       {'dry run' if self.dry_run else 'applied'}\n")
            fh.write(f"\nwritten={stats['inserted']} updated={stats['updated']} "
                     f"current={stats['current']} no_folder={stats['no_folder']} "
                     f"no_issue={stats['no_issue']} ambiguous={stats['ambiguous']} "
                     f"no_progress={stats['no_progress']} bad_data={stats['bad_data']} "
                     f"errors={stats['error']}\n")
            fh.write(f"page_count_mismatches={self.page_count_mismatches} "
                     f"unanalyzed_in_kavita={self.unanalyzed}\n")

            for title, explanation, counter in sections:
                if not counter:
                    continue
                fh.write(f"\n\n{title} ({sum(counter.values())} bookmarks, {len(counter)} series)\n")
                fh.write(f"{explanation}\n\n")
                for name, count in counter.most_common():
                    fh.write(f"  {count:5d}  {name}\n")

            if errors:
                fh.write(f"\n\nERRORS ({len(errors)})\n\n")
                for error in errors:
                    fh.write(f"  {error}\n")


def _parse_path_map(values: Optional[List[str]]) -> List[Tuple[str, str]]:
    """Parse --path-map OLD=NEW arguments, longest prefix first."""
    mappings: List[Tuple[str, str]] = []
    for value in values or []:
        if '=' not in value:
            raise ValueError(f"--path-map expects OLD=NEW, got '{value}'")
        old, _, new = value.partition('=')
        if not old.strip():
            raise ValueError(f"--path-map is missing the old prefix in '{value}'")
        mappings.append((old.rstrip('/'), new.rstrip('/')))
    return sorted(mappings, key=lambda m: len(m[0]), reverse=True)


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Migrate reading progress from a Codex reading server to Kavita',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Preview migration (dry run)
  python Codex_Migration.py --codex-db codex.sqlite --kavita-db kavita.db --username "myuser" --dry-run

  # Preview, saving the full list of what would be skipped
  python Codex_Migration.py --codex-db codex.sqlite --kavita-db kavita.db --username "myuser" --dry-run --report skipped.txt

  # Pick one of several Codex users
  python Codex_Migration.py --codex-db codex.sqlite --kavita-db kavita.db --username "myuser" --codex-user "them@example.com"

  # The two servers also reorganised the tree, not just the mount point
  python Codex_Migration.py --codex-db codex.sqlite --kavita-db kavita.db --username "myuser" --path-map "/comics/Marvel=/comics/Publishers/Marvel"

  # Execute migration
  python Codex_Migration.py --codex-db codex.sqlite --kavita-db kavita.db --username "myuser"

Library roots are read from each database, so the two servers do not need to mount
the library at the same path. Use --path-map only when the tree below the root differs.
        '''
    )

    parser.add_argument('--codex-db', required=True,
                        help='Path to the Codex SQLite database (typically codex.sqlite)')
    parser.add_argument('--kavita-db', required=True,
                        help='Path to Kavita SQLite database')
    parser.add_argument('--username', required=True,
                        help='Kavita username to migrate progress to')
    parser.add_argument('--codex-user', metavar='NAME',
                        help='Codex username to read progress from '
                             '(required only if the database holds more than one)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without modifying the database')
    parser.add_argument('--verbose', action='store_true',
                        help='Print a line for every bookmark instead of only the summary')
    parser.add_argument('--report', metavar='FILE',
                        help='Write the full list of skipped series to FILE')
    parser.add_argument('--path-map', metavar='OLD=NEW', action='append',
                        help='Rewrite a Codex path prefix before matching; repeatable')

    args = parser.parse_args()

    migrator = None
    try:
        migrator = CodexMigrator(
            codex_db_path=args.codex_db,
            kavita_db_path=args.kavita_db,
            username=args.username,
            codex_user=args.codex_user,
            dry_run=args.dry_run,
            verbose=args.verbose,
            report_path=args.report,
            path_map=_parse_path_map(args.path_map),
        )

        migrator.connect()
        migrator.migrate_progress()
        return 0

    except Exception as e:
        print(f"\nx Error: {e}", file=sys.stderr)
        return 1
    finally:
        if migrator is not None:
            migrator.disconnect()


if __name__ == '__main__':
    sys.exit(main())
