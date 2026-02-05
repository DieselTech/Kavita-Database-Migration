#!/usr/bin/env python3
"""
ComicRack to Kavita Migration Script

This script migrates reading progress from ComicRack XML database to a Kavita database.
ComicRack stores its data in XML format, in ComicDB.xml file.

The script matches comics by:
- Series name (normalized)
- Volume number/year
- Issue/Chapter number

Usage:
    python ComicRack_Migration.py --comicrack-xml /path/to/ComicDB.xml --kavita-db /path/to/kavita.db --username "YourKavitaUsername" [options]

Requirements:
    - Python 3.7+
    - No external dependencies
"""

import sqlite3
import argparse
import sys
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any


class ComicRackMigrator:
    """Handles migration of reading progress from ComicRack XML to Kavita database."""
    
    def __init__(self, comicrack_xml_path: str, kavita_db_path: str, username: str, dry_run: bool = False):
        """
        Initialize the ComicRack migrator.
        
        Args:
            comicrack_xml_path: Path to ComicRack XML database (typically ComicDB.xml)
            kavita_db_path: Path to Kavita SQLite database
            username: Kavita username to migrate progress to
            dry_run: If True, don't make any changes to the Kavita database
        """
        self.comicrack_xml_path = comicrack_xml_path
        self.kavita_db_path = kavita_db_path
        self.username = username
        self.dry_run = dry_run
        self.kavita_conn: Optional[sqlite3.Connection] = None
        self.user_id: Any = None
        
        # Mapping dictionaries
        self.series_mapping: Dict[str, int] = {}  # normalized_name -> series_id
        self.chapter_cache: Dict[Tuple[int, float], int] = {}  # (series_id, chapter_number) -> chapter_id
        
    def connect(self):
        """Connect to Kavita database and validate files."""
        print(f"Validating ComicRack XML: {os.path.basename(self.comicrack_xml_path)}")
        if not os.path.exists(self.comicrack_xml_path):
            raise FileNotFoundError(f"ComicRack XML not found: {self.comicrack_xml_path}")
        
        print(f"Connecting to Kavita database: {os.path.basename(self.kavita_db_path)}")
        if not os.path.exists(self.kavita_db_path):
            raise FileNotFoundError(f"Kavita database not found: {self.kavita_db_path}")
        
        self.kavita_conn = sqlite3.connect(self.kavita_db_path)
        self.kavita_conn.row_factory = sqlite3.Row
        
        # Find user ID
        cursor = self.kavita_conn.cursor()
        cursor.execute("SELECT Id FROM AspNetUsers WHERE UserName = ?", (self.username,))
        user_row = cursor.fetchone()
        
        if not user_row:
            raise ValueError(f"User '{self.username}' not found in Kavita database (try all lowercase)")
        
        self.user_id = user_row['Id']
        print(f"Found Kavita user: {self.username} (ID: {self.user_id})")
        
    def disconnect(self):
        """Disconnect from Kavita database."""
        if self.kavita_conn:
            if not self.dry_run:
                self.kavita_conn.commit()
            self.kavita_conn.close()

    def normalize_name(self, name: str, year: Optional[str] = None) -> str:
        """
        Normalize a series name for matching.
        This mimics Kavita's normalization logic.
        
        Kavita combines series name and year in the normalized name.
        For example: "Stained" (2017) becomes "stained2017"
        
        Args:
            name: The series name to normalize
            year: Optional year to append to the normalized name
            
        Returns:
            Normalized name (lowercase, alphanumeric only, possibly with year)
        """
        if not name:
            return ""
        # Remove special characters, keep alphanumeric only (no spaces for final form)
        normalized = re.sub(r'[^a-zA-Z0-9]', '', name)
        # Convert to lowercase
        normalized = normalized.lower()
        
        # If year is provided, append it to match Kavita's format
        if year:
            # Extract just the year digits
            year_str = re.sub(r'[^0-9]', '', str(year))
            if year_str:
                normalized += year_str
        
        return normalized

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
            # Remove timezone information from the end
            # Handle formats like: "2022-09-22T23:22:05.7422152Z" or "2022-09-22T23:22:05.7422152-05:00"
            
            # Replace 'Z' with empty string (UTC indicator)
            date_string = date_string.replace('Z', '')
            
            # Remove timezone offset like "-05:00" or "+05:00" from the end
            # Pattern: +/- followed by HH:MM at the end
            date_string = re.sub(r'[+-]\d{2}:\d{2}$', '', date_string)
            
            # ComicRack uses 7-digit microseconds (e.g., .7422152), but Python's %f only handles 6
            # Truncate to 6 digits if needed
            match = re.search(r'\.(\d{7,})', date_string)
            if match:
                microseconds = match.group(1)
                truncated = microseconds[:6]
                date_string = date_string.replace(f'.{microseconds}', f'.{truncated}')
            
            try:
                parsed_date = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S.%f')
            except ValueError:
                try:
                    parsed_date = datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    parsed_date = datetime.strptime(date_string, '%Y-%m-%d')
            
            return parsed_date
        except Exception as e:
            print(f"  Warning: Could not parse date '{date_string}': {e}")
            return None
    
    def build_series_mapping(self):
        """Build mapping of normalized series names to Kavita series IDs."""
        print("\n=== Building Series Mapping ===")

        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        cursor.execute("""
            SELECT Id, Name, NormalizedName, LocalizedName, NormalizedLocalizedName
            FROM Series
        """)
        
        series_list = cursor.fetchall()
        print(f"Found {len(series_list)} series in Kavita database")
        
        for series in series_list:
            series_id = series['Id']
            
            if series['NormalizedName']:
                norm_name = series['NormalizedName'].lower()
                if norm_name not in self.series_mapping:
                    self.series_mapping[norm_name] = series_id
            
            if series['NormalizedLocalizedName']:
                norm_local = series['NormalizedLocalizedName'].lower()
                if norm_local not in self.series_mapping:
                    self.series_mapping[norm_local] = series_id
            
            # Also try our own normalization for extra compatibility
            if series['Name']:
                our_norm = self.normalize_name(series['Name'])
                if our_norm and our_norm not in self.series_mapping:
                    self.series_mapping[our_norm] = series_id
        
        print(f"Created {len(self.series_mapping)} series name mappings")
    
    def find_chapter_id(self, series_id: int, chapter_number: float, volume_number: Optional[str] = None) -> Optional[int]:
        """
        Find a chapter ID in Kavita for a given series and chapter number.
        
        Args:
            series_id: The Kavita series ID
            chapter_number: The chapter/issue number
            volume_number: Optional volume number for additional matching
            
        Returns:
            Chapter ID if found, None otherwise
        """
        # Check cache first
        cache_key = (series_id, chapter_number)
        if cache_key in self.chapter_cache:
            return self.chapter_cache[cache_key]
        
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        
        # Try to find chapter by series and number
        cursor.execute("""
            SELECT c.Id, c.MinNumber, c.MaxNumber, v.Name as VolumeName
            FROM Chapter c
            JOIN Volume v ON c.VolumeId = v.Id
            WHERE v.SeriesId = ? 
            AND (c.MinNumber = ? OR (c.MinNumber <= ? AND c.MaxNumber >= ?))
            ORDER BY c.MinNumber
            LIMIT 1
        """, (series_id, chapter_number, chapter_number, chapter_number))
        
        result = cursor.fetchone()
        if result:
            chapter_id = result['Id']
            self.chapter_cache[cache_key] = chapter_id
            return chapter_id
        
        return None
    
    def parse_comicrack_xml(self) -> List[Dict[str, Any]]:
        """
        Parse ComicRack XML database and extract book entries with progress.
        
        Returns:
            List of dictionaries containing book data
        """
        print(f"\n=== Parsing ComicRack XML ===")
        print(f"Reading: {os.path.basename(self.comicrack_xml_path)}")
        
        try:
            tree = ET.parse(self.comicrack_xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            raise ValueError(f"Failed to parse ComicRack XML: {e}")
        
        books = []
        
        # Handle both <Books> and <ComicBooks> root elements
        book_elements = root.findall('.//Book') or root.findall('.//ComicBook')
        
        print(f"Found {len(book_elements)} books in ComicRack database")
        
        for book_elem in book_elements:
            book_data = {}
            
            # Extract all relevant fields
            for child in book_elem:
                tag = child.tag
                text = child.text
                
                # Convert text to appropriate type
                if text:
                    if tag in ['PageCount', 'CurrentPage', 'PagesRead', 'LastPageRead', 'FileSize']:
                        try:
                            book_data[tag] = int(text)
                        except ValueError:
                            book_data[tag] = text
                    elif tag in ['Number']:
                        try:
                            # Handle decimal numbers like "1.5"
                            book_data[tag] = float(text)
                        except ValueError:
                            book_data[tag] = text
                    elif tag == 'Opened':
                        # Store the date string as-is, we'll parse it later
                        book_data[tag] = text
                    else:
                        book_data[tag] = text
            
            # Only include books that have reading progress
            if book_data.get('PagesRead') or book_data.get('CurrentPage') or book_data.get('LastPageRead'):
                books.append(book_data)
        
        print(f"Found {len(books)} books with reading progress")
        return books
    
    def migrate_progress(self):
        """Migrate reading progress from ComicRack to Kavita."""
        print(f"\n=== Migrating Progress for User: {self.username} ===")
        
        books = self.parse_comicrack_xml()
        
        if not books:
            print("No books with progress found in ComicRack database")
            return
        
        # Build series mapping
        self.build_series_mapping()
        
        # Migrate each book's progress
        migrated = 0
        skipped = 0
        errors = []
        
        for book in books:
            try:
                result = self._migrate_book_progress(book)
                if result:
                    migrated += 1
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1
                series_name = book.get('Series', 'Unknown')
                number = book.get('Number', 'Unknown')
                error_msg = f"{series_name} #{number}: {str(e)}"
                errors.append(error_msg)
        
        # Print summary
        print(f"\n=== Migration Summary ===")
        print(f"Total books processed: {len(books)}")
        print(f"Successfully migrated: {migrated}")
        print(f"Skipped (no match or error): {skipped}")
        
        if errors:
            print(f"\n=== Errors ({len(errors)}) ===")
            for error in errors[:10]:  # Show first 10 errors
                print(f"  ✗ {error}")
            if len(errors) > 10:
                print(f"  ... and {len(errors) - 10} more errors")
    
    def _migrate_book_progress(self, book: Dict[str, Any]) -> bool:
        """
        Migrate progress for a single book.
        
        Args:
            book: Dictionary containing ComicRack book data
            
        Returns:
            True if migration successful, False if skipped
        """
        # Extract book information
        series_name = book.get('Series', '')
        number = book.get('Number')
        volume = book.get('Volume', '')
        year = book.get('Year', '')
        pages_read = book.get('PagesRead') or book.get('CurrentPage') or book.get('LastPageRead', 0)
        page_count = book.get('PageCount', 0)
        last_read_date_str = book.get('Opened', '')
        
        # If not available, fall back to current time
        # I don't love doing this, but it is better than nothing
        read_date = self.parse_comicrack_date(last_read_date_str)
        if not read_date:
            read_date = datetime.now()

        # Bind as string to avoid Python 3.12+ sqlite3 datetime adapter deprecation warning
        read_date_str = self._format_kavita_datetime(read_date)
        
        if not series_name:
            return False
        
        # Convert number to float
        try:
            if isinstance(number, str):
                chapter_number = float(number)
            elif isinstance(number, (int, float)):
                chapter_number = float(number)
            else:
                print(f"  ✗ {series_name}: Invalid number format: {number}")
                return False
        except (ValueError, TypeError):
            print(f"  ✗ {series_name}: Could not parse number: {number}")
            return False
        
        # Try to find series in Kavita using multiple strategies
        series_id = None
        
        # series name + year (Kavita's preferred format)
        if year:
            normalized_with_year = self.normalize_name(series_name, year)
            if normalized_with_year in self.series_mapping:
                series_id = self.series_mapping[normalized_with_year]
        
        # series name only as fallback
        if not series_id:
            normalized_series = self.normalize_name(series_name)
            if normalized_series in self.series_mapping:
                series_id = self.series_mapping[normalized_series]
        
        # volume as year if year is not provided but volume looks like a year
        if not series_id and volume and not year:
            # Check if volume is a 4-digit year
            if re.match(r'^\d{4}$', str(volume)):
                normalized_with_volume = self.normalize_name(series_name, volume)
                if normalized_with_volume in self.series_mapping:
                    series_id = self.series_mapping[normalized_with_volume]
        
        if not series_id:
            # Provide helpful debug information
            tried_names = [self.normalize_name(series_name, year) if year else None,
                          self.normalize_name(series_name)]
            tried_names = [n for n in tried_names if n]  # Remove None values
            print(f"  ✗ {series_name} #{chapter_number}: Series not found in Kavita (tried: {', '.join(tried_names)})")
            return False
        
        # Find chapter in Kavita
        chapter_id = self.find_chapter_id(series_id, chapter_number, volume)
        
        if not chapter_id:
            print(f"  ✗ {series_name} #{chapter_number}: Chapter not found in Kavita")
            return False
        
        # Get chapter details for progress
        assert self.kavita_conn is not None, "Not connected to Kavita database"
        cursor = self.kavita_conn.cursor()
        cursor.execute("""
            SELECT c.Id, c.Pages, c.VolumeId, v.SeriesId, s.LibraryId
            FROM Chapter c
            JOIN Volume v ON c.VolumeId = v.Id
            JOIN Series s ON v.SeriesId = s.Id
            WHERE c.Id = ?
        """, (chapter_id,))
        
        chapter_info = cursor.fetchone()
        if not chapter_info:
            return False
        
        chapter_pages = chapter_info['Pages']
        volume_id = chapter_info['VolumeId']
        library_id = chapter_info['LibraryId']
        
        # Calculate pages read (ensure it doesn't exceed actual pages)
        if pages_read > chapter_pages:
            pages_read = chapter_pages
        
        if self.dry_run:
            print(f"  ✓ [DRY RUN] Would migrate: {series_name} #{chapter_number} ({pages_read}/{chapter_pages} pages)")
            return True
        
        # Check if progress already exists
        cursor.execute("""
            SELECT Id, PagesRead FROM AppUserProgresses 
            WHERE AppUserId = ? AND ChapterId = ?
        """, (self.user_id, chapter_id))
        
        existing = cursor.fetchone()
        
        if existing:
            # Update if new progress is greater
            if pages_read > existing['PagesRead']:
                cursor.execute("""
                    UPDATE AppUserProgresses 
                    SET PagesRead = ?,
                        LastModified = ?,
                        LastModifiedUtc = ?
                    WHERE Id = ?
                """, (pages_read, read_date_str, read_date_str, existing['Id']))
                print(f"  ✓ Updated: {series_name} #{chapter_number} ({pages_read}/{chapter_pages} pages)")
            else:
                print(f"  → Skipped: {series_name} #{chapter_number} (existing progress is newer)")
                return False
        else:
            # Insert new progress
            cursor.execute("""
                INSERT INTO AppUserProgresses 
                (AppUserId, ChapterId, VolumeId, SeriesId, LibraryId, PagesRead, Created, LastModified, CreatedUtc, LastModifiedUtc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (self.user_id, chapter_id, volume_id, series_id, library_id, pages_read, read_date_str, read_date_str, read_date_str, read_date_str))
            
            print(f"  ✓ Migrated: {series_name} #{chapter_number} ({pages_read}/{chapter_pages} pages)")
        
        return True


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Migrate reading progress from ComicRack XML to Kavita database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Preview migration (dry run)
  python ComicRack_Migration.py --comicrack-xml ComicDB.xml --kavita-db kavita.db --username "myuser" --dry-run
  
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
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("ComicRack to Kavita Migration Script")
    print("=" * 60)
    
    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***\n")
    
    try:
        migrator = ComicRackMigrator(
            comicrack_xml_path=args.comicrack_xml,
            kavita_db_path=args.kavita_db,
            username=args.username,
            dry_run=args.dry_run
        )
        
        migrator.connect()
        migrator.migrate_progress()
        migrator.disconnect()
        
        print("\n=== Migration Complete ===")
        
        if args.dry_run:
            print("\nThis was a dry run. Run without --dry-run to apply changes.")
        
        return 0
        
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
