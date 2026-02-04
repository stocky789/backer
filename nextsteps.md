## Completed

### LOCAL directory repository backup path structure (FIXED)
When using local directory path as a storage repo, backups now go into the proper structure:
- `{local_path}/Agents/{job_name}/contents/` - actual backup files
- Kopia snapshots still created for versioning/point-in-time restore
- Repository scanning now discovers jobs from both filesystem and kopia snapshots

Changes made to `src/backer/server/app.py`:
1. `proxy_repo_backup` endpoint now extracts files to `{local_path}/Agents/{job_name}/contents/`
2. Files persist on disk (not deleted after kopia snapshot)
3. Kopia snapshot created from the contents directory for versioning
4. Added filesystem scanning for LOCAL repos to discover jobs from Agents/ folder

## Remaining work

### Unraid hypervisor backup
- Stub exists at `src/backer/hypervisors/unraid.py`
- Implementation pending

### Copy retention for LOCAL repos
- Currently only supported for SMB/NFS repos
- Need to add retention enforcement for LOCAL repos
