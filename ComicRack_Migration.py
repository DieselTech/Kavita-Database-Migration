#!/usr/bin/env python3
"""
ComicRack to Kavita Migration Script

This script migrates reading progress from a ComicRack XML database to a Kavita database.
ComicRack stores its data in XML format, in ComicDB.xml file.

The script matches comics by:
- Series name (normalized, with the trailing "(year)"/"(volume)" discriminator split off)
- Run identity (ComicRack's Volume ordinal / cover-date Year vs Kavita's series discriminator)
- Issue/Chapter number (numeric, ranged, or textual like "1.MU")

A ComicRack series name is usually ambiguous on its own ("Avengers" has dozens of runs). Candidates are therefore hard-gated on actually containing the issue number, then
scored on how well the run identity lines up. Ambiguous ties are reported. 

Usage:
    python ComicRack_Migration.py --comicrack-xml /path/to/ComicDB.xml --kavita-db /path/to/kavita.db --username "YourKavitaUsername" [options]

What I really recommend doing first:
    python ComicRack_Migration.py --comicrack-xml ComicDb.xml --kavita-db kavita.db --username $Name --dry-run --report skipped.txt

Requirements:
    - Python 3.7+
    - No external dependencies
"""

import sqlite3
import argparse
import sys
import os
import re
import collections
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

# Trailing "(2018)" / "(7)" discriminator Kavita appends to disambiguate runs.
DISCRIMINATOR_RE = re.compile(r'^(.*?)\s*\((\d{1,4})\)\s*$')

# A discriminator at or above this value is a publication year, below it is a run ordinal.
YEAR_THRESHOLD = 1900


class ComicRackMigrator:
    """Handles migration of reading progress from ComicRack XML to Kavita database."""

    def __init__(self, comicrack_xml_path: str, kavita_db_path: str, username: str,
                 dry_run: bool = False, verbose: bool = False,
                 report_path: Optional[str] = None):
        """
        Initialize the ComicRack migrator.

        Args:
            comicrack_xml_path: Path to ComicRack XML database (typically ComicDB.xml)
            kavita_db_path: Path to Kavita SQLite database
            username: Kavita username to migrate progress to
            dry_run: If True, don't make any changes to the Kavita database
            verbose: If True, print a line for every book instead of only failures
            report_path: Optional file to write the full skip breakdown to
        """
        self.comicrack_xml_path = comicrack_xml_path
        self.kavita_db_path = kavita_db_path
        self.username = username
        self.dry_run = dry_run
        self.verbose = verbose
        self.report_path = report_path
        self.kavita_conn: Optional[sqlite3.Connection] = None
        self.user_id: Any = None

        # base normalized name -> list of candidate series dicts
        self.series_index: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        # series_id -> {'num': {float: chapter}, 'ranged': [chapter], 'text': {str: chapter}}
        self.chapter_index: Dict[int, Dict[str, Any]] = {}
        # Reasons books were skipped, for the end-of-run report
        self.unmatched_series: collections.Counter = collections.Counter()
        self.unmatched_chapter: collections.Counter = collections.Counter()
        self.conflicts: collections.Counter = collections.Counter()
        # Chapters a dry run would have written, so previews match a real run exactly
        self._pending: Dict[int, int] = {}
        self.comicrack_book_count = 0
        self.kavita_series_count = 0
        self.kavita_chapter_count = 0

    def connect(self):
        """Connect to Kavita database and validate files."""
        if not os.path.exists(self.comicrack_xml_path):
            raise FileNotFoundError(f"ComicRack XML not found: {self.comicrack_xml_path}")

        if not os.path.exists(self.kavita_db_path):
            raise FileNotFoundError(f"Kavita database not found: {self.kavita_db_path}")

        self.kavita_conn = sqlite3.connect(self.kavita_db_path)
        self.kavita_conn.row_factory = sqlite3.Row

        # Find user ID
        cursor = self.kavita_conn.cursor()
        cursor.execute("SELECT Id FROM AspNetUsers WHERE UserName = ?", (self.username,))
        user_row = cursor.fetchone()

        if not user_row:
            cursor.execute("SELECT UserName FROM AspNetUsers ORDER BY UserName")
            known = ', '.join(r['UserName'] for r in cursor.fetchall())
            raise ValueError(f"User '{self.username}' not found in Kavita database. Known users: {known}")

        self.user_id = user_row['Id']

    def disconnect(self):
        """Disconnect from Kavita database."""
        if self.kavita_conn:
            if not self.dry_run:
                self.kavita_conn.commit()
            self.kavita_conn.close()
            self.kavita_conn = None

    # ------------------------------------------------------------------ naming

    def normalize_name(self, name: str, year: Optional[str] = None) -> str:
        """
        Normalize a series name for matching. This mimics Kavita's normalization logic.

        Kavita combines series name and year in the normalized name, so
        "Stained" (2017) becomes "stained2017".

        Args:
            name: The series name to normalize
            year: Optional year to append to the normalized name

        Returns:
            Normalized name (lowercase, alphanumeric only, possibly with year)
        """
        if not name:
            return ""
        normalized = re.sub(r'[^a-zA-Z0-9]', '', name).lower()

        if year:
            year_str = re.sub(r'[^0-9]', '', str(year))
            if year_str:
                normalized += year_str

        return normalized

    def name_keys(self, name: str) -> List[str]:
        """
        Build the lookup keys for a series name, without any discriminator.

        Returns the plain normalized name plus a leading-article-stripped variant, so
        "Punisher: War Zone" also finds Kavita's "The Punisher: War Zone (1992)".
        """
        keys = []
        base = self.normalize_name(name)
        if base:
            keys.append(base)
        stripped = re.sub(r'^\s*(the|a|an)\s+', '', (name or '').strip(), flags=re.IGNORECASE)
        alt = self.normalize_name(stripped)
        if alt and alt not in keys:
            keys.append(alt)
        return keys

    @staticmethod
    def split_discriminator(name: str) -> Tuple[str, Optional[int]]:
        """Split "Avengers (2023)" into ("Avengers", 2023). Returns (name, None) if absent."""
        match = DISCRIMINATOR_RE.match(name or '')
        if match:
            return match.group(1), int(match.group(2))
        return name or '', None

    # ------------------------------------------------------------------- dates

    @staticmethod
    def _format_kavita_datetime(dt: datetime) -> str:
        """Format a datetime for Kavita's SQLite fields without relying on sqlite3's datetime."""
        if dt.microsecond:
            # Trim trailing zeros to avoid overly long strings
            return dt.strftime('%Y-%m-%d %H:%M:%S.%f').rstrip('0').rstrip('.')
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def parse_comicrack_date(self, date_string: str) -> Optional[datetime]:
        """
        Parse ComicRack date string and remove timezone information.

        ComicRack stores dates with timezone offsets:
        - "2022-09-22T23:22:05.7422152Z" (UTC)
        - "2022-09-22T23:22:05.7422152-05:00" (with offset)

        Kavita expects dates without timezone information as it handles
        timezone conversion based on user's environment settings.

        Args:
            date_string: The date string from ComicRack XML

        Returns:
            datetime object without timezone info, or None if parsing fails
        """
        if not date_string:
            return None

        try:
            # Drop the UTC marker and any "+05:00" / "-05:00" trailing offset
            date_string = date_string.replace('Z', '')
            date_string = re.sub(r'[+-]\d{2}:\d{2}$', '', date_string)

            # ComicRack uses 7-digit fractional seconds; Python's %f only handles 6
            match = re.search(r'\.(\d{7,})', date_string)
            if match:
                microseconds = match.group(1)
                date_string = date_string.replace(f'.{microseconds}', f'.{microseconds[:6]}')

            for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
                try:
                    return datetime.strptime(date_string, fmt)
                except ValueError:
                    continue
            raise ValueError(f"no known format matched")
        except Exception as e:
            print(f"  Warning: Could not parse date '{date_string}': {e}")
            return None

    # ----------------------------------------------------------------- indexes

    def build_series_index(self):
        """Index Kavita series by base name, keeping every run as a separate candidate."""
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        cursor.execute("SELECT Id, Name, NormalizedName, LocalizedName FROM Series")

        series_list = cursor.fetchall()
        for series in series_list:
            for raw_name in (series['Name'], series['LocalizedName']):
                if not raw_name:
                    continue
                base, disc = self.split_discriminator(raw_name)
                candidate = {
                    'id': series['Id'],
                    'name': series['Name'],
                    'disc': disc,
                    'is_year': disc is not None and disc >= YEAR_THRESHOLD,
                    'normalized': (series['NormalizedName'] or '').lower(),
                }
                for key in self.name_keys(base):
                    bucket = self.series_index[key]
                    if not any(c['id'] == candidate['id'] for c in bucket):
                        bucket.append(candidate)

        self.kavita_series_count = len(series_list)

    def build_chapter_index(self):
        """Load every chapter once, indexed by series, for numeric/ranged/text lookup."""
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        cursor.execute("""
            SELECT v.SeriesId, c.Id, c.MinNumber, c.MaxNumber, c.Number, c.Range,
                   c.Pages, c.VolumeId
            FROM Chapter c
            JOIN Volume v ON c.VolumeId = v.Id
            ORDER BY v.SeriesId, c.MinNumber
        """)

        total = 0
        for row in cursor:
            series_id = row['SeriesId']
            idx = self.chapter_index.get(series_id)
            if idx is None:
                idx = {'num': {}, 'ranged': [], 'text': {}}
                self.chapter_index[series_id] = idx

            chapter = {
                'id': row['Id'],
                'pages': row['Pages'],
                'volume_id': row['VolumeId'],
                'min': row['MinNumber'],
                'max': row['MaxNumber'],
            }
            total += 1

            # Text form ("1.MU", "16.HU"), which Kavita stores with MinNumber == 0
            for text_field in (row['Range'], row['Number']):
                if not text_field:
                    continue
                key = str(text_field).strip().lower()
                if key and key not in idx['text']:
                    idx['text'][key] = chapter

            # Numeric form: trust Range when it parses, else a single-issue Min/Max
            numeric = None
            if row['Range']:
                try:
                    numeric = float(str(row['Range']).strip())
                except ValueError:
                    numeric = None
            if numeric is None and row['MinNumber'] == row['MaxNumber'] and not row['Range']:
                numeric = row['MinNumber']

            if numeric is not None:
                idx['num'].setdefault(numeric, chapter)
            if row['MaxNumber'] > row['MinNumber']:
                idx['ranged'].append(chapter)

        self.kavita_chapter_count = total

    # ---------------------------------------------------------------- matching

    def find_chapter(self, series_id: int, number_text: Optional[str],
                     number_value: Optional[float]) -> Optional[Dict[str, Any]]:
        """
        Find a chapter within one Kavita series.

        Text numbers ("1.MU") are tried first because Kavita stores them with MinNumber 0,
        which would otherwise collide with a genuine issue #0.
        """
        idx = self.chapter_index.get(series_id)
        if not idx:
            return None

        if number_text:
            hit = idx['text'].get(number_text.strip().lower())
            if hit:
                return hit

        if number_value is not None:
            hit = idx['num'].get(number_value)
            if hit:
                return hit
            for chapter in idx['ranged']:
                if chapter['min'] <= number_value <= chapter['max']:
                    return chapter

        return None

    def score_candidate(self, candidate: Dict[str, Any], year: Optional[int],
                        vol_ordinal: Optional[int], vol_year: Optional[int]) -> int:
        """
        Score how well a Kavita run matches a ComicRack book's run identity.

        ComicRack's Year is the issue's cover date, not the run's start year, so an exact
        year hit is strong evidence but a run that merely *started* before this issue is
        the usual correct answer. Closer start years beat older ones.
        """
        disc = candidate['disc']

        if disc is None:
            # Bare-name series in Kavita are typically stubs; only take them as a last resort
            return -20

        if candidate['is_year']:
            if vol_year is not None and disc == vol_year:
                return 120
            if year is not None and disc == year:
                return 100
            if year is not None:
                if disc <= year:
                    return 60 - min(50, year - disc)
                return -40 - min(50, disc - year)
            return 0

        # Ordinal discriminator, e.g. "Black Panther (7)" is volume 7
        if vol_ordinal is not None and disc == vol_ordinal:
            return 110
        return -10

    # ------------------------------------------------------------------ parsing

    def parse_comicrack_xml(self) -> List[Dict[str, Any]]:
        """
        Parse ComicRack XML database and extract book entries with progress.

        Returns:
            List of dictionaries containing book data
        """
        try:
            tree = ET.parse(self.comicrack_xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            raise ValueError(f"Failed to parse ComicRack XML: {e}")

        books = []

        # Handle both <Book> and <ComicBook> entries
        book_elements = root.findall('.//Book') or root.findall('.//ComicBook')

        self.comicrack_book_count = len(book_elements)

        for book_elem in book_elements:
            book_data: Dict[str, Any] = {}

            for child in book_elem:
                tag = child.tag
                text = child.text
                if not text:
                    continue

                if tag in ('PageCount', 'CurrentPage', 'PagesRead', 'LastPageRead', 'FileSize'):
                    try:
                        book_data[tag] = int(text)
                    except ValueError:
                        pass  # Non-numeric page counts are unusable; drop rather than poison
                else:
                    # Number, Volume, Year, Opened and the rest stay as raw text
                    book_data[tag] = text

            # Only include books that have reading progress
            if book_data.get('PagesRead') or book_data.get('CurrentPage') or book_data.get('LastPageRead'):
                books.append(book_data)

        return books

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        """Best-effort int from a ComicRack field, returning None instead of raising."""
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    # ---------------------------------------------------------------- migration

    def candidates_for(self, series_name: str) -> List[Dict[str, Any]]:
        """Every Kavita run whose base name matches this ComicRack series name."""
        out: List[Dict[str, Any]] = []
        seen = set()
        for key in self.name_keys(series_name):
            for candidate in self.series_index.get(key, []):
                if candidate['id'] not in seen:
                    seen.add(candidate['id'])
                    out.append(candidate)
        return out

    def migrate_progress(self):
        """Migrate reading progress from ComicRack to Kavita."""
        mode = "DRY RUN (no changes written)" if self.dry_run else "APPLYING CHANGES"
        print(f"ComicRack -> Kavita  |  user: {self.username}  |  {mode}")
        print(f"  source: {self.comicrack_xml_path}")
        print(f"  target: {self.kavita_db_path}\n")

        books = self.parse_comicrack_xml()

        if not books:
            print("No books with progress found in ComicRack database")
            return

        self.build_series_index()
        self.build_chapter_index()

        stats: collections.Counter = collections.Counter()
        errors: List[str] = []

        # Phase 1: read the books; nothing is matched or written yet
        parsed: List[Dict[str, Any]] = []
        for book in books:
            try:
                item = self._parse_book(book)
                if item is None:
                    stats['bad_data'] += 1
                else:
                    parsed.append(item)
            except Exception as e:
                stats['error'] += 1
                errors.append(f"{book.get('Series', 'Unknown')} #{book.get('Number', '?')}: {e}")

        # Phase 2: pick the best Kavita run for each book, gated on it holding the issue
        claims: List[Dict[str, Any]] = []
        for item in parsed:
            candidates = self.candidates_for(item['series_name'])
            if not candidates:
                self.unmatched_series[item['series_name']] += 1
                stats['no_series'] += 1
                continue

            best: Optional[Tuple[Tuple, int, Dict, Dict]] = None
            for candidate in candidates:
                chapter = self.find_chapter(
                    candidate['id'], item['number_text'], item['number_value']
                )
                if chapter is None:
                    continue
                score = self.score_candidate(
                    candidate, item['year'], item['vol_ordinal'], item['vol_year']
                )
                # Highest score wins; tie-break toward the most recent run, then a stable id
                sort_key = (score, candidate['disc'] or 0, -candidate['id'])
                if best is None or sort_key > best[0]:
                    best = (sort_key, score, candidate, chapter)

            if best is None:
                self.unmatched_chapter[item['series_name']] += 1
                stats['no_chapter'] += 1
                continue

            _, score, candidate, chapter = best
            chapter_pages = chapter['pages']
            item.update({
                'candidate': candidate,
                'chapter': chapter,
                'score': score,
                'pages_read': min(item['pages_read'], chapter_pages) if chapter_pages else item['pages_read'],
            })
            claims.append(item)

        # Phase 3: arbitrate chapters claimed by more than one run
        stats['conflict'] += self._resolve_conflicts(claims)

        # Phase 4: write the surviving claims
        for item in claims:
            if item.get('rejected'):
                continue
            try:
                stats[self._write_progress(item)] += 1
            except Exception as e:
                stats['error'] += 1
                errors.append(f"{item['series_name']} #{item['number_text']}: {e}")

        self._print_summary(len(books), stats, errors)

    def _resolve_conflicts(self, claims: List[Dict[str, Any]]) -> int:
        """
        Reject claims where two *different* ComicRack runs land on the same Kavita chapter.

        One run appearing twice is a duplicate file and merges normally (highest page count
        wins at write time). Two different runs colliding means one of them has no matching
        run in Kavita and fell back onto a neighbour's issues - keeping the better-scoring
        claim and reporting the other beats silently writing bad progress. Only the
        overlapping issues are dropped, so the loser keeps everything it alone claims.
        """
        by_chapter: Dict[int, List[Dict[str, Any]]] = collections.defaultdict(list)
        for item in claims:
            by_chapter[item['chapter']['id']].append(item)

        rejected = 0
        for group in by_chapter.values():
            if len(group) < 2 or len({c['run_key'] for c in group}) < 2:
                continue
            best = max(c['score'] for c in group)
            winners = {c['run_key'] for c in group if c['score'] == best}
            # A tie between distinct runs is unresolvable evidence; drop them all
            keep = next(iter(winners)) if len(winners) == 1 else None
            for claim in group:
                if claim['run_key'] != keep:
                    claim['rejected'] = True
                    rejected += 1
                    self.conflicts[claim['series_name']] += 1
        return rejected

    def _print_summary(self, total: int, stats: collections.Counter, errors: List[str]):
        """Print a short, actionable report of what the run did."""
        matched = stats['inserted'] + stats['updated'] + stats['current']
        skipped = total - matched

        print(f"Read    {total} books with reading progress "
              f"({self.comicrack_book_count} total in ComicRack)")
        print(f"Indexed {self.kavita_series_count} series / "
              f"{self.kavita_chapter_count} chapters from Kavita")

        verb = "Would match" if self.dry_run else "Matched"
        rate = f" ({matched / total:.1%})" if total else ""
        print(f"\n{verb} {matched} of {total} books{rate}")

        # "existing progress" may be a row already in Kavita or one this same run just
        # wrote for a duplicate ComicRack entry, so the wording stays neutral about which.
        if self.dry_run:
            labels = ("new", "would raise existing progress", "already at or beyond this point")
        else:
            labels = ("written", "raised existing progress", "left as-is, already further along")
        detail = [f"{stats[k]} {label}"
                  for k, label in zip(('inserted', 'updated', 'current'), labels)
                  if stats[k]]
        if detail:
            print("        " + ", ".join(detail))

        # Skip reasons, largest first, with a few example series inline
        reasons = [
            (stats['no_series'], "series not in Kavita", self.unmatched_series),
            (stats['conflict'], "ambiguous run, not guessed", self.conflicts),
            (stats['bad_data'], "no series/number in ComicRack", None),
            (stats['no_chapter'], "issue not in Kavita", self.unmatched_chapter),
            (stats['error'], "errors", None),
        ]
        reasons = [r for r in reasons if r[0]]

        if skipped:
            print(f"\nSkipped {skipped} books")
            for count, label, counter in reasons:
                line = f"  {count:5d}  {label:<28}"
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
            ("SERIES NOT IN KAVITA",
             "No series of this name exists in the Kavita library.",
             self.unmatched_series),
            ("AMBIGUOUS RUN - NOT GUESSED",
             "Two different ComicRack runs matched the same Kavita issue. The better-scoring\n"
             "run was kept; these were skipped rather than written to a possibly wrong issue.\n"
             "Usually means the correct run is missing from Kavita.",
             self.conflicts),
            ("ISSUE NOT IN KAVITA",
             "The series exists in Kavita, but not this issue number.",
             self.unmatched_chapter),
        ]
        with open(self.report_path, 'w', encoding='utf-8') as fh:
            fh.write(f"ComicRack -> Kavita migration report\n")
            fh.write(f"Generated:  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"User:       {self.username}\n")
            fh.write(f"Source:     {self.comicrack_xml_path}\n")
            fh.write(f"Target:     {self.kavita_db_path}\n")
            fh.write(f"Mode:       {'dry run' if self.dry_run else 'applied'}\n")
            fh.write(f"\nwritten={stats['inserted']} updated={stats['updated']} "
                     f"current={stats['current']} no_series={stats['no_series']} "
                     f"conflict={stats['conflict']} no_chapter={stats['no_chapter']} "
                     f"bad_data={stats['bad_data']} errors={stats['error']}\n")

            for title, explanation, counter in sections:
                if not counter:
                    continue
                fh.write(f"\n\n{title} ({sum(counter.values())} books, {len(counter)} series)\n")
                fh.write(f"{explanation}\n\n")
                for name, count in counter.most_common():
                    fh.write(f"  {count:5d}  {name}\n")

            if errors:
                fh.write(f"\n\nERRORS ({len(errors)})\n\n")
                for error in errors:
                    fh.write(f"  {error}\n")

    def _parse_book(self, book: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Pull the fields needed for matching out of one ComicRack book.

        Returns:
            A dict of parsed fields, or None when the book carries too little metadata
        """
        series_name = book.get('Series', '')
        if not series_name:
            return None

        number_raw = book.get('Number')
        if number_raw is None or str(number_raw).strip() == '':
            return None
        number_text = str(number_raw).strip()
        try:
            number_value: Optional[float] = float(number_text)
        except ValueError:
            number_value = None  # Textual issue like "1.MU"; resolved via Chapter.Range

        year = self._as_int(book.get('Year'))
        volume = self._as_int(book.get('Volume'))
        vol_year = volume if volume is not None and volume >= YEAR_THRESHOLD else None
        vol_ordinal = volume if volume is not None and volume < YEAR_THRESHOLD else None

        pages_read = self._as_int(
            book.get('PagesRead') or book.get('CurrentPage') or book.get('LastPageRead')
        )
        if pages_read is None or pages_read <= 0:
            return None

        # If the read date is unavailable, fall back to current time.
        # I don't love doing this, but it is better than nothing.
        read_date = self.parse_comicrack_date(book.get('Opened', '')) or datetime.now()

        return {
            'series_name': series_name,
            'number_text': number_text,
            'number_value': number_value,
            'year': year,
            'vol_year': vol_year,
            'vol_ordinal': vol_ordinal,
            'pages_read': pages_read,
            # Identity of the ComicRack run, used to tell duplicates from genuine collisions
            'run_key': (self.normalize_name(series_name), book.get('Volume')),
            # Bind as string to avoid Python 3.12+ sqlite3 datetime adapter deprecation warning
            'read_date_str': self._format_kavita_datetime(read_date),
        }

    def _write_progress(self, item: Dict[str, Any]) -> str:
        """
        Write one resolved claim to Kavita.

        Returns:
            One of: inserted, updated, current
        """
        candidate = item['candidate']
        chapter = item['chapter']
        series_id = candidate['id']
        chapter_pages = chapter['pages']
        pages_read = item['pages_read']
        read_date_str = item['read_date_str']

        label = f"{item['series_name']} #{item['number_text']} -> {candidate['name']}"

        # Read the existing row even on a dry run, so the preview counts are truthful
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        cursor.execute("""
            SELECT Id, PagesRead FROM AppUserProgresses
            WHERE AppUserId = ? AND ChapterId = ?
        """, (self.user_id, chapter['id']))
        existing = cursor.fetchone()

        prefix = '[DRY RUN] ' if self.dry_run else ''
        existing_pages = existing['PagesRead'] if existing else None

        # A real run sees its own uncommitted writes; a dry run has to simulate them, or
        # duplicate ComicRack entries would all look like fresh inserts.
        if self.dry_run and chapter['id'] in self._pending:
            simulated = self._pending[chapter['id']]
            existing_pages = simulated if existing_pages is None else max(existing_pages, simulated)

        if existing_pages is not None:
            if pages_read <= existing_pages:
                if self.verbose:
                    print(f"  = {prefix}{label} (Kavita already at {existing_pages} pages)")
                return 'current'
            if self.dry_run:
                self._pending[chapter['id']] = pages_read
                if self.verbose:
                    print(f"  ^ {prefix}Would update: {label} ({existing_pages} -> {pages_read}/{chapter_pages} pages)")
                return 'updated'
            cursor.execute("""
                UPDATE AppUserProgresses
                SET PagesRead = ?,
                    LastModified = ?,
                    LastModifiedUtc = ?
                WHERE Id = ?
            """, (pages_read, read_date_str, read_date_str, existing['Id']))
            if self.verbose:
                print(f"  ^ Updated: {label} ({pages_read}/{chapter_pages} pages)")
            return 'updated'

        if self.dry_run:
            self._pending[chapter['id']] = pages_read
            if self.verbose:
                print(f"  + {prefix}Would migrate: {label} ({pages_read}/{chapter_pages} pages)")
            return 'inserted'

        cursor.execute("""
            INSERT INTO AppUserProgresses
            (AppUserId, ChapterId, VolumeId, SeriesId, LibraryId, PagesRead, Created, LastModified, CreatedUtc, LastModifiedUtc)
            SELECT ?, ?, ?, ?, s.LibraryId, ?, ?, ?, ?, ?
            FROM Series s WHERE s.Id = ?
        """, (self.user_id, chapter['id'], chapter['volume_id'], series_id, pages_read,
              read_date_str, read_date_str, read_date_str, read_date_str, series_id))
        if self.verbose:
            print(f"  + Migrated: {label} ({pages_read}/{chapter_pages} pages)")
        return 'inserted'


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Migrate reading progress from ComicRack XML to Kavita database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Preview migration (dry run)
  python ComicRack_Migration.py --comicrack-xml ComicDB.xml --kavita-db kavita.db --username "myuser" --dry-run

  # Preview, saving the full list of what would be skipped
  python ComicRack_Migration.py --comicrack-xml ComicDB.xml --kavita-db kavita.db --username "myuser" --dry-run --report skipped.txt

  # Execute migration
  python ComicRack_Migration.py --comicrack-xml ComicDB.xml --kavita-db kavita.db --username "myuser"
        '''
    )

    parser.add_argument('--comicrack-xml', required=True,
                        help='Path to ComicRack XML database file (typically ComicDB.xml)')
    parser.add_argument('--kavita-db', required=True,
                        help='Path to Kavita SQLite database')
    parser.add_argument('--username', required=True,
                        help='Kavita username to migrate progress to')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without modifying the database')
    parser.add_argument('--verbose', action='store_true',
                        help='Print a line for every book instead of only the summary')
    parser.add_argument('--report', metavar='FILE',
                        help='Write the full list of skipped series to FILE')

    args = parser.parse_args()

    migrator = None
    try:
        migrator = ComicRackMigrator(
            comicrack_xml_path=args.comicrack_xml,
            kavita_db_path=args.kavita_db,
            username=args.username,
            dry_run=args.dry_run,
            verbose=args.verbose,
            report_path=args.report,
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
