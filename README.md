# Kavita-Database-Migration
Python script to help migrate from an old database to a fresh new database while still bringing over your read progress and users. 

Usage:
    python Kavita_Migrate_Progress.py --old-db /path/to/old/kavita.db --new-db /path/to/new/kavita.db [--dry_run]

Requirements:
    - Python 3.7+
    - No external dependencies

This script migrates users and their progress events / settings from an old Kavita database
to a new Kavita database. It handles user accounts, reading progress, bookmarks,
reading sessions, and other user-related data.

The migration script allows you to:
- Transfer user accounts from an old Kavita database to a new one
- Migrate user preferences and settings
- Transfer reading progress and bookmarks
- Migrate reading sessions and history
- Map content between old and new libraries

This is useful when:
- Setting up a new Kavita instance with reorganized libraries
- Consolidating multiple Kavita instances
- Recovering from database corruption
- Migrating to a new server with different file paths

There is some steps to make this script work or run properly: 

1. Shut down your old install of Kavita that has all your historical data in it. 
2. Move your database out of Kavita's config folder and put it in a safe location.
3. Start up your fresh new instance of Kavita you will be migrating to
4. Create the new admin user
5. Add back your libraries you want your instance to have. Use the same names as they had before, but they don't have to be in the same locations.
   -ie; If your old instance had a "Manga" library, the new instance should be "Manga"
7. Once the scan finishes, shut down the new instance.
8. Move the new instance database to the local machine where the script resides
9. Run the script and point it at both files.
10. Watch as it maps the libraries, then the files, then asks you what users you want to move over.
11. Once it is completed your new kavita database will have the mapped progress and read events in it.
12. Take your new database file and put it back in your Kavita instance
13. Start Kavita. 

Do NOT run this script over the network or SMB share. Both databases must be local files.

## What Gets Migrated

### User Account Data
- Username and email
- Password hash (user can log in with same password)
- User roles (Admin, User, etc.)
- Library Access: Access to the same libraries (if they exist in new database)
- API Keys: OPDS and integration API keys for external applications
- Age restrictions
- External service tokens (AniList, MAL)
- Profile settings

### Reading Data
- Progress Events: Pages read per chapter
- Bookmarks: Saved pages with thumbnails
- Reading Sessions: Time spent reading
- Reading History: Daily reading statistics
- Ratings: Series and chapter ratings
- Reviews: User reviews of series

### What Doesn't Get Migrated (But might in the future)
- Reading Lists (user-created)
- Collections (user-created)
- Want to Read lists
- Annotations/highlights
