#!/usr/bin/env python3
"""
Kavita Database Migration Script

This script migrates users and their progress events / settings from an old Kavita database
to a new Kavita database. It handles user accounts, reading progress, bookmarks,
reading sessions, and other user-related data.

Do NOT run this script over the network or SMB share. Both databases must be local files.

Usage:
    python Kavita_Migrate_Progress.py --old-db /path/to/old/kavita.db --new-db /path/to/new/kavita.db [dry_run]

Requirements:
    - Python 3.7+
    - No external dependencies
"""

import sqlite3
import argparse
import sys
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any


class DatabaseMigrator:
    """Handles migration of users and progress from old to new Kavita database."""
    
    def __init__(self, old_db_path: str, new_db_path: str, dry_run: bool = False):
        """
        Initialize the database migrator.
        
        Args:
            old_db_path: Path to the old Kavita database
            new_db_path: Path to the new Kavita database
            dry_run: If True, don't make any changes to the new database
        """
        self.old_db_path = old_db_path
        self.new_db_path = new_db_path
        self.dry_run = dry_run
        self.old_conn = None
        self.new_conn = None
        
        # Mapping dictionaries
        self.user_mapping: Dict[int, int] = {}  # old_user_id -> new_user_id
        self.library_mapping: Dict[int, int] = {}  # old_library_id -> new_library_id
        self.series_mapping: Dict[int, int] = {}  # old_series_id -> new_series_id
        self.volume_mapping: Dict[int, int] = {}  # old_volume_id -> new_volume_id
        self.chapter_mapping: Dict[int, int] = {}  # old_chapter_id -> new_chapter_id
        self.file_mapping: Dict[int, int] = {}  # old_file_id -> new_file_id
        
    def connect(self):
        """Connect to both databases."""
        print(f"Connecting to old database: {self.old_db_path}")
        if not os.path.exists(self.old_db_path):
            raise FileNotFoundError(f"Old database not found: {self.old_db_path}")
        self.old_conn = sqlite3.connect(self.old_db_path)
        self.old_conn.row_factory = sqlite3.Row
        
        print(f"Connecting to new database: {self.new_db_path}")
        if not os.path.exists(self.new_db_path):
            raise FileNotFoundError(f"New database not found: {self.new_db_path}")
        self.new_conn = sqlite3.connect(self.new_db_path)
        self.new_conn.row_factory = sqlite3.Row
        
    def disconnect(self):
        """Disconnect from both databases."""
        if self.old_conn:
            self.old_conn.close()
        if self.new_conn:
            if not self.dry_run:
                self.new_conn.commit()
            self.new_conn.close()
            
    def get_table_columns(self, conn: sqlite3.Connection, table_name: str) -> List[str]:
        """
        Get list of column names for a table.
        """
        # Validate table_name: only alphanumeric and underscores allowed
        if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
            raise ValueError(f"Invalid table name: {table_name}")
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]
        
    def build_insert_query(self, table_name: str, columns: List[str]) -> Tuple[str, str, str]:
        """
        Build a safe INSERT query with parameterized placeholders.
        
        Args:
            table_name: The table name (must be alphanumeric with underscores)
            columns: List of column names from get_table_columns()
            
        Returns:
            Tuple of (columns_str, placeholders, full_query)
        """
        if not re.match(r'^[a-zA-Z0-9_]+$', table_name):
            raise ValueError(f"Invalid table name: {table_name}")
            
        # Validate column names: should only contain alphanumeric and underscores from schema
        for col in columns:
            if not re.match(r'^[a-zA-Z0-9_]+$', col):
                raise ValueError(f"Invalid column name: {col}")
        
        # Quote column names to handle SQL reserved keywords like "Order"
        quoted_columns = [f'"{col}"' for col in columns]
        columns_str = ', '.join(quoted_columns)
        placeholders = ', '.join(['?' for _ in columns])
        query = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"
        
        return columns_str, placeholders, query
        
    def map_libraries(self):
        """Map libraries from old database to new database by name."""
        print("\n=== Mapping Libraries ===")
        
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        old_cursor.execute("SELECT Id, Name, Type FROM Library ORDER BY Name")
        old_libraries = old_cursor.fetchall()
        
        new_cursor.execute("SELECT Id, Name, Type FROM Library ORDER BY Name")
        new_libraries = {row['Name']: row for row in new_cursor.fetchall()}
        
        for old_lib in old_libraries:
            old_id = old_lib['Id']
            old_name = old_lib['Name']
            old_type = old_lib['Type']
            
            if old_name in new_libraries:
                new_lib = new_libraries[old_name]
                if new_lib['Type'] == old_type:
                    self.library_mapping[old_id] = new_lib['Id']
                    print(f"  ✓ Mapped library '{old_name}' (Old ID: {old_id} -> New ID: {new_lib['Id']})")
                else:
                    print(f"  ✗ Library '{old_name}' type mismatch (Old: {old_type}, New: {new_lib['Type']})")
            else:
                print(f"  ✗ Library '{old_name}' not found in new database")
                
        print(f"\nMapped {len(self.library_mapping)} out of {len(old_libraries)} libraries")
        
    def map_series(self):
        """Map series from old database to new database by name and library."""
        print("\n=== Mapping Series ===")
        
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        # Only map series from mapped libraries
        if not self.library_mapping:
            print("  No libraries mapped, skipping series mapping")
            return
            
        for old_lib_id, new_lib_id in self.library_mapping.items():
            old_cursor.execute("""
                SELECT Id, Name, NormalizedName, LocalizedName, LibraryId 
                FROM Series 
                WHERE LibraryId = ?
                ORDER BY Name
            """, (old_lib_id,))
            old_series = old_cursor.fetchall()
            
            new_cursor.execute("""
                SELECT Id, Name, NormalizedName, LocalizedName, LibraryId 
                FROM Series 
                WHERE LibraryId = ?
                ORDER BY Name
            """, (new_lib_id,))
            new_series_list = new_cursor.fetchall()
            
            # Create lookup by normalized name (primary key)
            new_series_by_normalized = {}
            new_series_by_localized = {}
            
            for ns in new_series_list:
                new_series_by_normalized[ns['NormalizedName']] = ns
                # Only add to localized lookup if it doesn't exist yet (avoid overwrites)
                if ns['LocalizedName']:
                    localized_key = ns['LocalizedName'].lower()
                    if localized_key not in new_series_by_localized:
                        new_series_by_localized[localized_key] = ns
                    # If there's a collision, we won't use localized name for mapping
                    
            for old_s in old_series:
                old_id = old_s['Id']
                old_name = old_s['Name']
                old_normalized = old_s['NormalizedName']
                
                # Try to find matching series by normalized name (preferred method)
                matched = False
                if old_normalized in new_series_by_normalized:
                    new_s = new_series_by_normalized[old_normalized]
                    self.series_mapping[old_id] = new_s['Id']
                    print(f"  ✓ Mapped series '{old_name}' (Old ID: {old_id} -> New ID: {new_s['Id']})")
                    matched = True
                # Fallback to localized name only if normalized name didn't match
                elif old_s['LocalizedName'] and old_s['LocalizedName'].lower() in new_series_by_localized:
                    localized_key = old_s['LocalizedName'].lower()
                    new_s = new_series_by_localized[localized_key]
                    self.series_mapping[old_id] = new_s['Id']
                    print(f"  ✓ Mapped series '{old_name}' via localized name (Old ID: {old_id} -> New ID: {new_s['Id']})")
                    matched = True
                    
                if not matched:
                    print(f"  ✗ Series '{old_name}' not found in new database")
                    
        print(f"\nMapped {len(self.series_mapping)} series")
        
        # Validate mappings - check for duplicates (multiple old IDs to same new ID)
        self._validate_series_mappings()
    
    def _validate_series_mappings(self):
        """Validate series mappings to detect potential issues."""
        # Check for duplicate mappings (multiple old series mapping to same new series)
        reverse_mapping = {}
        duplicates = []
        
        for old_id, new_id in self.series_mapping.items():
            if new_id in reverse_mapping:
                duplicates.append((old_id, reverse_mapping[new_id], new_id))
            else:
                reverse_mapping[new_id] = old_id
        
        if duplicates:
            print("\n  ⚠ WARNING: Detected duplicate mappings (multiple old series -> same new series):")
            old_cursor = self.old_conn.cursor()
            new_cursor = self.new_conn.cursor()
            
            for old_id1, old_id2, new_id in duplicates:
                # Get series names for better debugging
                old_cursor.execute("SELECT Name FROM Series WHERE Id = ?", (old_id1,))
                old_row1 = old_cursor.fetchone()
                old_name1 = old_row1['Name'] if old_row1 else 'Unknown'
                
                old_cursor.execute("SELECT Name FROM Series WHERE Id = ?", (old_id2,))
                old_row2 = old_cursor.fetchone()
                old_name2 = old_row2['Name'] if old_row2 else 'Unknown'
                
                new_cursor.execute("SELECT Name FROM Series WHERE Id = ?", (new_id,))
                new_row = new_cursor.fetchone()
                new_name = new_row['Name'] if new_row else 'Unknown'
                
                print(f"    Old '{old_name1}' (ID: {old_id1}) and '{old_name2}' (ID: {old_id2}) "
                      f"both map to '{new_name}' (ID: {new_id})")
            
            print("  This will cause incorrect progress/rating data. Please review the series mapping.")
        
    def map_volumes_and_chapters(self):
        """Map volumes and chapters from old database to new database."""
        print("\n=== Mapping Volumes and Chapters ===")
        
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        if not self.series_mapping:
            print("  No series mapped, skipping volume and chapter mapping")
            return
            
        for old_series_id, new_series_id in self.series_mapping.items():
            old_cursor.execute("""
                SELECT Id, Name, MinNumber, MaxNumber 
                FROM Volume 
                WHERE SeriesId = ?
                ORDER BY MinNumber
            """, (old_series_id,))
            old_volumes = old_cursor.fetchall()
            
            new_cursor.execute("""
                SELECT Id, Name, MinNumber, MaxNumber 
                FROM Volume 
                WHERE SeriesId = ?
                ORDER BY MinNumber
            """, (new_series_id,))
            new_volumes = {(row['MinNumber'], row['MaxNumber']): row for row in new_cursor.fetchall()}
            
            for old_vol in old_volumes:
                old_id = old_vol['Id']
                key = (old_vol['MinNumber'], old_vol['MaxNumber'])
                
                if key in new_volumes:
                    new_vol = new_volumes[key]
                    self.volume_mapping[old_id] = new_vol['Id']
                    
                    # Map chapters in this volume
                    old_cursor.execute("""
                        SELECT Id, MinNumber, MaxNumber, Range, IsSpecial 
                        FROM Chapter 
                        WHERE VolumeId = ?
                        ORDER BY MinNumber
                    """, (old_id,))
                    old_chapters = old_cursor.fetchall()
                    
                    new_cursor.execute("""
                        SELECT Id, MinNumber, MaxNumber, Range, IsSpecial 
                        FROM Chapter 
                        WHERE VolumeId = ?
                        ORDER BY MinNumber
                    """, (new_vol['Id'],))
                    new_chapters = {}
                    for row in new_cursor.fetchall():
                        ch_key = (row['MinNumber'], row['MaxNumber'], row['IsSpecial'])
                        new_chapters[ch_key] = row
                    
                    for old_ch in old_chapters:
                        old_ch_id = old_ch['Id']
                        ch_key = (old_ch['MinNumber'], old_ch['MaxNumber'], old_ch['IsSpecial'])
                        
                        if ch_key in new_chapters:
                            new_ch = new_chapters[ch_key]
                            self.chapter_mapping[old_ch_id] = new_ch['Id']
                            
        print(f"Mapped {len(self.volume_mapping)} volumes and {len(self.chapter_mapping)} chapters")
        
    def map_files(self):
        """
        Map manga files from old database to new database by file path.
        
        Note: Files are matched by basename within already-mapped chapters, so
        there's minimal risk of incorrect matches since files are scoped to chapters.
        """
        print("\n=== Mapping Files ===")
        
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        if not self.chapter_mapping:
            print("  No chapters mapped, skipping file mapping")
            return
            
        for old_chapter_id, new_chapter_id in self.chapter_mapping.items():
            old_cursor.execute("""
                SELECT Id, FilePath, FileName 
                FROM MangaFile 
                WHERE ChapterId = ?
            """, (old_chapter_id,))
            old_files = old_cursor.fetchall()
            
            new_cursor.execute("""
                SELECT Id, FilePath, FileName 
                FROM MangaFile 
                WHERE ChapterId = ?
            """, (new_chapter_id,))
            # Match by basename since files are already scoped to the same chapter
            new_files = {os.path.basename(row['FilePath']): row for row in new_cursor.fetchall()}
            
            for old_file in old_files:
                old_id = old_file['Id']
                filename = os.path.basename(old_file['FilePath'])
                
                if filename in new_files:
                    new_file = new_files[filename]
                    self.file_mapping[old_id] = new_file['Id']
                    
        print(f"Mapped {len(self.file_mapping)} files")
        
    def get_users(self) -> List[sqlite3.Row]:
        """Get all users from old database."""
        cursor = self.old_conn.cursor()
        cursor.execute("""
            SELECT Id, UserName, Email, NormalizedUserName, NormalizedEmail,
                   PasswordHash, SecurityStamp, ConcurrencyStamp,
                   Created, CreatedUtc, LastActive, LastActiveUtc,
                   AgeRestriction, AgeRestrictionIncludeUnknowns,
                   AniListAccessToken, MalUserName, MalAccessToken,
                   ConfirmationToken, OidcId, IdentityProvider,
                   CoverImage, PrimaryColor, SecondaryColor
            FROM AspNetUsers
            ORDER BY UserName
        """)
        return cursor.fetchall()
        
    def check_user_exists(self, username: str) -> Optional[int]:
        """Check if user already exists in new database."""
        cursor = self.new_conn.cursor()
        cursor.execute("SELECT Id FROM AspNetUsers WHERE UserName = ?", (username,))
        row = cursor.fetchone()
        return row['Id'] if row else None
        
    def migrate_user(self, user: sqlite3.Row) -> Optional[int]:
        """
        Migrate a single user to the new database.
        
        Returns:
            New user ID if successful, None otherwise
        """
        old_id = user['Id']
        username = user['UserName']
        
        existing_id = self.check_user_exists(username)
        if existing_id:
            print(f"  User '{username}' already exists in new database (ID: {existing_id})")
            response = input(f"  Use existing user? (y/n): ").strip().lower()
            if response == 'y':
                self.user_mapping[old_id] = existing_id
                return existing_id
            else:
                print(f"  Skipping user '{username}'")
                return None
                
        # Get columns for AspNetUsers table in new database
        new_columns = self.get_table_columns(self.new_conn, "AspNetUsers")
        
        # Default values for required Identity framework columns
        identity_defaults = {
            'AccessFailedCount': 0,
            'LockoutEnabled': True,
            'PhoneNumberConfirmed': False,
            'TwoFactorEnabled': False,
            'EmailConfirmed': True,
            'LockoutEnd': None,
            'PhoneNumber': None,
            'RowVersion': 0  # Concurrency token, starts at 0
        }
        
        # Build insert statement with only columns that exist in new database
        columns_to_insert = []
        values_to_insert = []
        
        for col in new_columns:
            if col == 'Id':
                continue  # Skip ID, let database auto-generate
            elif col in user.keys():
                # Use value from old database if available
                columns_to_insert.append(col)
                values_to_insert.append(user[col])
            elif col in identity_defaults:
                # Use default value for missing Identity framework columns
                columns_to_insert.append(col)
                values_to_insert.append(identity_defaults[col])
                
        if self.dry_run:
            print(f"  [DRY RUN] Would create user '{username}'")
            return -1  # Dummy ID for dry run
            
        # Build safe INSERT query using helper
        _, _, insert_query = self.build_insert_query("AspNetUsers", columns_to_insert)
        
        cursor = self.new_conn.cursor()
        cursor.execute(insert_query, values_to_insert)
        
        new_user_id = cursor.lastrowid
        self.user_mapping[old_id] = new_user_id
        
        print(f"  ✓ Created user '{username}' (Old ID: {old_id} -> New ID: {new_user_id})")
        
        return new_user_id
        
    def migrate_user_roles(self, old_user_id: int, new_user_id: int):
        """Migrate user roles."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        # Get roles from old database
        old_cursor.execute("""
            SELECT RoleId 
            FROM AspNetUserRoles 
            WHERE UserId = ?
        """, (old_user_id,))
        old_roles = old_cursor.fetchall()
        
        for old_role in old_roles:
            role_id = old_role['RoleId']
            
            # Check if role exists in new database
            new_cursor.execute("SELECT Id FROM AspNetRoles WHERE Id = ?", (role_id,))
            if new_cursor.fetchone():
                try:
                    new_cursor.execute("""
                        INSERT OR IGNORE INTO AspNetUserRoles (UserId, RoleId)
                        VALUES (?, ?)
                    """, (new_user_id, role_id))
                except sqlite3.IntegrityError:
                    pass  # Role already assigned
    
    def migrate_user_library_access(self, old_user_id: int, new_user_id: int):
        """Migrate user library access permissions."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        junction_tables = ['LibraryAppUser', 'AppUserLibrary']
        junction_table = None
        
        for table_name in junction_tables:
            try:
                old_cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
                if old_cursor.fetchone():
                    junction_table = table_name
                    break
            except sqlite3.Error:
                continue
        
        if not junction_table:
            print(f"    ⚠ Could not find library junction table, skipping library access migration")
            return
        
        # Get columns in the junction table to handle different naming conventions
        old_cursor.execute(f"PRAGMA table_info({junction_table})")
        columns = [row[1] for row in old_cursor.fetchall()]
        
        library_col = None
        user_col = None
        
        for col in columns:
            if 'librar' in col.lower():
                library_col = col
            if 'user' in col.lower() or 'appuser' in col.lower():
                user_col = col
        
        if not library_col or not user_col:
            print(f"    ⚠ Could not determine junction table columns, skipping library access migration")
            return
        
        # Get libraries from old database that the user has access to
        old_cursor.execute(f"""
            SELECT {library_col} 
            FROM {junction_table} 
            WHERE {user_col} = ?
        """, (old_user_id,))
        old_library_ids = old_cursor.fetchall()
        
        migrated = 0
        skipped = 0
        
        for row in old_library_ids:
            old_library_id = row[0]
            
            # Check if we have a mapping for this library
            if old_library_id not in self.library_mapping:
                skipped += 1
                continue
            
            new_library_id = self.library_mapping[old_library_id]
            
            # Insert library access into new database
            try:
                new_cursor.execute(f"""
                    INSERT OR IGNORE INTO {junction_table} ({user_col}, {library_col})
                    VALUES (?, ?)
                """, (new_user_id, new_library_id))
                migrated += 1
            except sqlite3.IntegrityError:
                pass  # Access already exists
        
        if migrated > 0:
            print(f"    ✓ Migrated access to {migrated} libraries (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} libraries (not found in new database)")
                    
    def migrate_user_preferences(self, old_user_id: int, new_user_id: int):
        """Migrate user preferences."""
        old_cursor = self.old_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserPreferences WHERE AppUserId = ?
        """, (old_user_id,))
        old_prefs = old_cursor.fetchone()
        
        if not old_prefs:
            return
            
        if self.dry_run:
            print(f"    [DRY RUN] Would migrate preferences")
            return
            
        # Get columns for new database
        new_columns = self.get_table_columns(self.new_conn, "AppUserPreferences")
        
        columns_to_insert = []
        values_to_insert = []
        
        for col in new_columns:
            if col == 'AppUserId':
                columns_to_insert.append(col)
                values_to_insert.append(new_user_id)
            elif col in old_prefs.keys() and col != 'Id':
                columns_to_insert.append(col)
                values_to_insert.append(old_prefs[col])
        
        cursor = self.new_conn.cursor()
        
        # Check if preferences already exist
        cursor.execute("SELECT Id FROM AppUserPreferences WHERE AppUserId = ?", (new_user_id,))
        if cursor.fetchone():
            print(f"    ✓ Preferences already exist for user")
            return
            
        _, _, insert_query = self.build_insert_query("AppUserPreferences", columns_to_insert)
        cursor.execute(insert_query, values_to_insert)
        
        print(f"    ✓ Migrated user preferences")
        
    def migrate_user_progress(self, old_user_id: int, new_user_id: int):
        """Migrate user progress events."""
        old_cursor = self.old_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserProgresses WHERE AppUserId = ?
        """, (old_user_id,))
        old_progress_list = old_cursor.fetchall()
        
        if not old_progress_list:
            return
            
        migrated = 0
        skipped = 0
        
        for old_progress in old_progress_list:
            # Check if we have mappings for this progress
            old_chapter_id = old_progress['ChapterId']
            old_volume_id = old_progress['VolumeId']
            old_series_id = old_progress['SeriesId']
            old_library_id = old_progress['LibraryId']
            
            if (old_chapter_id not in self.chapter_mapping or
                old_volume_id not in self.volume_mapping or
                old_series_id not in self.series_mapping or
                old_library_id not in self.library_mapping):
                skipped += 1
                continue
                
            new_chapter_id = self.chapter_mapping[old_chapter_id]
            new_volume_id = self.volume_mapping[old_volume_id]
            new_series_id = self.series_mapping[old_series_id]
            new_library_id = self.library_mapping[old_library_id]
            
            if self.dry_run:
                migrated += 1
                continue
                
            # Get columns for new database
            new_columns = self.get_table_columns(self.new_conn, "AppUserProgresses")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col == 'ChapterId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_chapter_id)
                elif col == 'VolumeId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_volume_id)
                elif col == 'SeriesId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_series_id)
                elif col == 'LibraryId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_library_id)
                elif col in old_progress.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_progress[col])
            
            cursor = self.new_conn.cursor()
            
            # Check if progress already exists
            cursor.execute("""
                SELECT Id FROM AppUserProgresses 
                WHERE AppUserId = ? AND ChapterId = ?
            """, (new_user_id, new_chapter_id))
            
            if cursor.fetchone():
                skipped += 1
                continue
                
            try:
                _, _, insert_query = self.build_insert_query("AppUserProgresses", columns_to_insert)
                cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating progress: {e}")
                skipped += 1
                
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} progress events (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} progress events (no matching content)")
            
    def migrate_user_bookmarks(self, old_user_id: int, new_user_id: int):
        """Migrate user bookmarks."""
        old_cursor = self.old_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserBookmark WHERE AppUserId = ?
        """, (old_user_id,))
        old_bookmarks = old_cursor.fetchall()
        
        if not old_bookmarks:
            return
            
        migrated = 0
        skipped = 0

        # Check if we have mappings
        for old_bookmark in old_bookmarks:
            old_chapter_id = old_bookmark['ChapterId']
            old_volume_id = old_bookmark['VolumeId']
            old_series_id = old_bookmark['SeriesId']
            
            if (old_chapter_id not in self.chapter_mapping or
                old_volume_id not in self.volume_mapping or
                old_series_id not in self.series_mapping):
                skipped += 1
                continue
                
            new_chapter_id = self.chapter_mapping[old_chapter_id]
            new_volume_id = self.volume_mapping[old_volume_id]
            new_series_id = self.series_mapping[old_series_id]
            
            if self.dry_run:
                migrated += 1
                continue
                
            # Get columns
            new_columns = self.get_table_columns(self.new_conn, "AppUserBookmark")
            
            # Build insert
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col == 'ChapterId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_chapter_id)
                elif col == 'VolumeId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_volume_id)
                elif col == 'SeriesId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_series_id)
                elif col in old_bookmark.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_bookmark[col])
            
            cursor = self.new_conn.cursor()
            
            try:
                _, _, insert_query = self.build_insert_query("AppUserBookmark", columns_to_insert)
                cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating bookmark: {e}")
                skipped += 1
                
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} bookmarks (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} bookmarks (no matching content)")
            
    def migrate_user_ratings(self, old_user_id: int, new_user_id: int):
        """Migrate user ratings."""
        old_cursor = self.old_conn.cursor()
        
        # Series ratings
        old_cursor.execute("""
            SELECT * FROM AppUserRating WHERE AppUserId = ?
        """, (old_user_id,))
        old_ratings = old_cursor.fetchall()
        
        migrated = 0
        skipped = 0
        
        for old_rating in old_ratings:
            old_series_id = old_rating['SeriesId']
            
            if old_series_id not in self.series_mapping:
                skipped += 1
                continue
                
            new_series_id = self.series_mapping[old_series_id]
            
            if self.dry_run:
                migrated += 1
                continue
                
            cursor = self.new_conn.cursor()
            
            # Check if rating exists
            cursor.execute("""
                SELECT Id FROM AppUserRating 
                WHERE AppUserId = ? AND SeriesId = ?
            """, (new_user_id, new_series_id))
            
            if cursor.fetchone():
                skipped += 1
                continue
            
            # Get columns for new database
            new_columns = self.get_table_columns(self.new_conn, "AppUserRating")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col == 'SeriesId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_series_id)
                elif col in old_rating.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_rating[col])
                
            try:
                _, _, insert_query = self.build_insert_query("AppUserRating", columns_to_insert)
                cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError:
                skipped += 1
                
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} series ratings (skipped {skipped})")
            
    def migrate_reading_sessions(self, old_user_id: int, new_user_id: int):
        """Migrate reading sessions and activity data."""
        old_cursor = self.old_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserReadingSession WHERE AppUserId = ?
        """, (old_user_id,))
        old_sessions = old_cursor.fetchall()
        
        if not old_sessions:
            return
            
        migrated = 0
        
        for old_session in old_sessions:
            if self.dry_run:
                migrated += 1
                continue
                
            new_columns = self.get_table_columns(self.new_conn, "AppUserReadingSession")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col in old_session.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_session[col])
            
            cursor = self.new_conn.cursor()
            
            try:
                _, _, insert_query = self.build_insert_query("AppUserReadingSession", columns_to_insert)
                cursor.execute(insert_query, values_to_insert)
                new_session_id = cursor.lastrowid
                
                # Migrate activity data
                old_cursor.execute("""
                    SELECT * FROM AppUserReadingSessionActivityData 
                    WHERE AppUserReadingSessionId = ?
                """, (old_session['Id'],))
                old_activities = old_cursor.fetchall()
                
                for old_activity in old_activities:
                    # Check if we have mappings
                    if 'SeriesId' in old_activity.keys():
                        old_series_id = old_activity['SeriesId']
                        if old_series_id and old_series_id not in self.series_mapping:
                            continue
                        new_series_id = self.series_mapping.get(old_series_id)
                    else:
                        new_series_id = None
                        
                    if 'ChapterId' in old_activity.keys():
                        old_chapter_id = old_activity['ChapterId']
                        if old_chapter_id and old_chapter_id not in self.chapter_mapping:
                            continue
                        new_chapter_id = self.chapter_mapping.get(old_chapter_id)
                    else:
                        new_chapter_id = None
                        
                    if 'LibraryId' in old_activity.keys():
                        old_library_id = old_activity['LibraryId']
                        if old_library_id and old_library_id not in self.library_mapping:
                            continue
                        new_library_id = self.library_mapping.get(old_library_id)
                    else:
                        new_library_id = None
                        
                    activity_columns = self.get_table_columns(self.new_conn, "AppUserReadingSessionActivityData")
                    
                    act_cols = []
                    act_vals = []
                    
                    for col in activity_columns:
                        if col == 'AppUserReadingSessionId':
                            act_cols.append(col)
                            act_vals.append(new_session_id)
                        elif col == 'SeriesId' and new_series_id:
                            act_cols.append(col)
                            act_vals.append(new_series_id)
                        elif col == 'ChapterId' and new_chapter_id:
                            act_cols.append(col)
                            act_vals.append(new_chapter_id)
                        elif col == 'LibraryId' and new_library_id:
                            act_cols.append(col)
                            act_vals.append(new_library_id)
                        elif col in old_activity.keys() and col != 'Id':
                            act_cols.append(col)
                            act_vals.append(old_activity[col])
                            
                    if act_cols:
                        try:
                            _, _, insert_query = self.build_insert_query("AppUserReadingSessionActivityData", act_cols)
                            cursor.execute(insert_query, act_vals)
                        except sqlite3.IntegrityError:
                            pass
                            
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating reading session: {e}")
                
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} reading sessions")
    
    def migrate_user_side_nav_streams(self, old_user_id: int, new_user_id: int):
        """Migrate user side navigation streams."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserSideNavStream WHERE AppUserId = ?
        """, (old_user_id,))
        old_streams = old_cursor.fetchall()
        
        if not old_streams:
            return
            
        migrated = 0
        skipped = 0
        
        for old_stream in old_streams:
            # Check if similar stream already exists for this user
            # Match by Name and StreamType to avoid duplicates
            if 'Name' in old_stream.keys() and 'StreamType' in old_stream.keys():
                new_cursor.execute("""
                    SELECT Id FROM AppUserSideNavStream 
                    WHERE AppUserId = ? AND "Name" = ? AND StreamType = ?
                """, (new_user_id, old_stream['Name'], old_stream['StreamType']))
                if new_cursor.fetchone():
                    skipped += 1
                    continue
            
            new_columns = self.get_table_columns(self.new_conn, "AppUserSideNavStream")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col == 'LibraryId' and 'LibraryId' in old_stream.keys():
                    # Map library ID if it exists
                    old_library_id = old_stream['LibraryId']
                    if old_library_id and old_library_id in self.library_mapping:
                        columns_to_insert.append(col)
                        values_to_insert.append(self.library_mapping[old_library_id])
                    elif old_library_id:
                        # Skip this stream if library doesn't exist in new database
                        skipped += 1
                        break
                elif col == 'Visible' and col not in old_stream.keys():
                    # Default Visible to True for older databases that don't have this column
                    columns_to_insert.append(col)
                    values_to_insert.append(True)
                elif col in old_stream.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_stream[col])
            
            # Only insert if we didn't skip due to missing library
            if len(columns_to_insert) > 0 and skipped == migrated + skipped:
                try:
                    _, _, insert_query = self.build_insert_query("AppUserSideNavStream", columns_to_insert)
                    new_cursor.execute(insert_query, values_to_insert)
                    migrated += 1
                except sqlite3.IntegrityError as e:
                    print(f"    ✗ Error migrating side nav stream: {e}")
                    skipped += 1
        
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} side nav streams (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} side nav streams (already exist or libraries not found)")
    
    def migrate_user_dashboard_streams(self, old_user_id: int, new_user_id: int):
        """Migrate user dashboard streams."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserDashboardStream WHERE AppUserId = ?
        """, (old_user_id,))
        old_streams = old_cursor.fetchall()
        
        if not old_streams:
            return
            
        migrated = 0
        skipped = 0
        
        for old_stream in old_streams:
            # Check if similar stream already exists for this user
            # Match by Name and StreamType to avoid duplicates
            if 'Name' in old_stream.keys() and 'StreamType' in old_stream.keys():
                new_cursor.execute("""
                    SELECT Id FROM AppUserDashboardStream 
                    WHERE AppUserId = ? AND "Name" = ? AND StreamType = ?
                """, (new_user_id, old_stream['Name'], old_stream['StreamType']))
                if new_cursor.fetchone():
                    skipped += 1
                    continue
            
            new_columns = self.get_table_columns(self.new_conn, "AppUserDashboardStream")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col in old_stream.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_stream[col])
            
            try:
                _, _, insert_query = self.build_insert_query("AppUserDashboardStream", columns_to_insert)
                new_cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating dashboard stream: {e}")
                skipped += 1
        
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} dashboard streams (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} dashboard streams (already exist)")
    
    def migrate_user_auth_keys(self, old_user_id: int, new_user_id: int):
        """Migrate user API auth keys."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserAuthKey WHERE AppUserId = ?
        """, (old_user_id,))
        old_keys = old_cursor.fetchall()
        
        if not old_keys:
            return
            
        migrated = 0
        skipped = 0
        
        for old_key in old_keys:
            new_columns = self.get_table_columns(self.new_conn, "AppUserAuthKey")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col in old_key.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_key[col])
            
            cursor = self.new_conn.cursor()
            
            # Check if this key already exists (by Key value)
            if 'Key' in old_key.keys():
                cursor.execute("""
                    SELECT Id FROM AppUserAuthKey WHERE "Key" = ?
                """, (old_key['Key'],))
                if cursor.fetchone():
                    skipped += 1
                    continue
            
            try:
                _, _, insert_query = self.build_insert_query("AppUserAuthKey", columns_to_insert)
                cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating auth key: {e}")
                skipped += 1
        
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} API keys (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} API keys (already exist)")
    
    def migrate_user_smart_filters(self, old_user_id: int, new_user_id: int):
        """Migrate user smart filters."""
        if self.dry_run:
            return
            
        old_cursor = self.old_conn.cursor()
        new_cursor = self.new_conn.cursor()
        
        old_cursor.execute("""
            SELECT * FROM AppUserSmartFilter WHERE AppUserId = ?
        """, (old_user_id,))
        old_filters = old_cursor.fetchall()
        
        if not old_filters:
            return
            
        migrated = 0
        skipped = 0
        
        for old_filter in old_filters:
            # Check if filter with same name already exists for this user
            if 'Name' in old_filter.keys():
                new_cursor.execute("""
                    SELECT Id FROM AppUserSmartFilter 
                    WHERE AppUserId = ? AND "Name" = ?
                """, (new_user_id, old_filter['Name']))
                if new_cursor.fetchone():
                    skipped += 1
                    continue
            
            new_columns = self.get_table_columns(self.new_conn, "AppUserSmartFilter")
            
            columns_to_insert = []
            values_to_insert = []
            
            for col in new_columns:
                if col == 'AppUserId':
                    columns_to_insert.append(col)
                    values_to_insert.append(new_user_id)
                elif col in old_filter.keys() and col != 'Id':
                    columns_to_insert.append(col)
                    values_to_insert.append(old_filter[col])
            
            try:
                _, _, insert_query = self.build_insert_query("AppUserSmartFilter", columns_to_insert)
                new_cursor.execute(insert_query, values_to_insert)
                migrated += 1
            except sqlite3.IntegrityError as e:
                print(f"    ✗ Error migrating smart filter: {e}")
                skipped += 1
        
        if migrated > 0:
            print(f"    ✓ Migrated {migrated} smart filters (skipped {skipped})")
        elif skipped > 0:
            print(f"    ⚠ Skipped {skipped} smart filters (already exist)")
            
    def migrate_user_data(self, old_user_id: int, new_user_id: int):
        """Migrate all data for a user."""
        print(f"  Migrating data for user ID {old_user_id} -> {new_user_id}")
        
        self.migrate_user_roles(old_user_id, new_user_id)
        self.migrate_user_library_access(old_user_id, new_user_id)
        self.migrate_user_preferences(old_user_id, new_user_id)
        self.migrate_user_auth_keys(old_user_id, new_user_id)
        self.migrate_user_smart_filters(old_user_id, new_user_id)
        self.migrate_user_side_nav_streams(old_user_id, new_user_id)
        self.migrate_user_dashboard_streams(old_user_id, new_user_id)
        self.migrate_user_progress(old_user_id, new_user_id)
        self.migrate_user_bookmarks(old_user_id, new_user_id)
        self.migrate_user_ratings(old_user_id, new_user_id)
        self.migrate_reading_sessions(old_user_id, new_user_id)
        
    def run(self):
        """Run the migration process."""
        try:
            self.connect()
            
            print("\n" + "="*60)
            print("Kavita Database Migration Tool")
            print("="*60)
            
            self.map_libraries()
            self.map_series()
            self.map_volumes_and_chapters()
            self.map_files()
            
            print("\n=== Migrating Users ===")
            users = self.get_users()
            
            if not users:
                print("No users found in old database")
                return
                
            print(f"Found {len(users)} users in old database\n")
            
            for user in users:
                username = user['UserName']
                print(f"\nUser: {username}")
                print(f"  Email: {user['Email']}")
                print(f"  Created: {user['Created']}")
                print(f"  Last Active: {user['LastActive']}")
                
                response = input(f"  Migrate this user? (y/n/q): ").strip().lower()
                
                if response == 'q':
                    print("\nMigration cancelled by user")
                    break
                elif response == 'y':
                    new_user_id = self.migrate_user(user)
                    if new_user_id and new_user_id > 0:  # Skip if None or dry-run dummy ID
                        self.migrate_user_data(user['Id'], new_user_id)
                else:
                    print(f"  Skipped user '{username}'")
                    
            # Summary
            print("\n" + "="*60)
            print("Migration Summary")
            print("="*60)
            print(f"Libraries mapped: {len(self.library_mapping)}")
            print(f"Series mapped: {len(self.series_mapping)}")
            print(f"Volumes mapped: {len(self.volume_mapping)}")
            print(f"Chapters mapped: {len(self.chapter_mapping)}")
            print(f"Files mapped: {len(self.file_mapping)}")
            print(f"Users migrated: {len(self.user_mapping)}")
            
            if self.dry_run:
                print("\n[DRY RUN MODE] No changes were made to the database")
            else:
                print("\nMigration completed successfully!")
                
        except FileNotFoundError as e:
            print(f"\nError: {e}")
        except sqlite3.Error as e:
            print(f"\nDatabase error: {e}")
            if not self.dry_run:
                print("\nRolling back changes...")
                self.new_conn.rollback()
        except Exception as e:
            print(f"\nUnexpected error during migration: {type(e).__name__}: {e}")
            print("For detailed debugging information, run the script with Python in debug mode.")
            if not self.dry_run:
                print("\nRolling back changes...")
                self.new_conn.rollback()
        finally:
            self.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description='Migrate users and progress from old Kavita database to new one',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic migration
  python migrate_database.py --old-db /path/to/old/kavita.db --new-db /path/to/new/kavita.db
  
  # Dry run (no changes made)
  python migrate_database.py --old-db old.db --new-db new.db --dry-run
  
Notes:
  - Always backup your databases before running this script
  - The script will prompt for confirmation before migrating each user
  - Series and libraries are matched by name
  - Progress events are only migrated if matching content exists in new database
        """
    )
    
    parser.add_argument(
        '--old-db',
        required=True,
        help='Path to the old Kavita database file'
    )
    
    parser.add_argument(
        '--new-db',
        required=True,
        help='Path to the new Kavita database file'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run in dry-run mode (no changes made to new database)'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.old_db):
        print(f"Error: Old database not found: {args.old_db}")
        sys.exit(1)
        
    if not os.path.exists(args.new_db):
        print(f"Error: New database not found: {args.new_db}")
        sys.exit(1)
        
    print("\n⚠️  WARNING: Always backup your databases before running this script!")
    print("\n⚠️  WARNING: Do NOT point to a database over the network or SMB share! Both databases must be local files.\n")
    
    if not args.dry_run:
        response = input("\nHave you backed up your databases? (yes/no): ").strip().lower()
        if response != 'yes':
            print("Please backup your databases first, then run the script again.")
            sys.exit(0)
    
    migrator = DatabaseMigrator(args.old_db, args.new_db, args.dry_run)
    migrator.run()


if __name__ == '__main__':
    main()
