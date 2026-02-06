# Update Project Documentation

Update project documentation after making changes.

## Instructions

1. Get current timestamp:
   ```bash
   date '+%Y-%m-%d %H:%M:%S'
   ```

2. Update `docs/memory/changelog.md`:
   - Add entry at the TOP of "Recent Changes" section
   - Use appropriate emoji prefix:
     - 🎉 Major milestones
     - ✨ New features
     - 🔧 Bug fixes
     - 🔄 Refactoring
     - 📝 Documentation
     - 🔒 Security updates
     - 🚀 Performance improvements
     - 💾 Data/persistence changes
     - 🐳 Docker/infrastructure
   - Include what changed and why

3. If API/schema/integration changed:
   - Update `docs/memory/architecture.md`
   - Add new endpoints to API table
   - Update database schema if changed
   - Update architecture diagram if needed

4. If feature behavior changed:
   - Use the `feature-flow-analyzer` agent to update impacted feature flows
   - Example: `Task tool with subagent_type=feature-flow-analyzer`
   - The agent will automatically analyze and update relevant flow documents in `docs/memory/feature-flows/`

5. If feature scope changed:
   - Update `docs/memory/requirements.md`
   - Mark requirements as completed if done
   - Add new requirements if scope expanded

6. Update `docs/memory/project_index.json` if:
   - New feature implemented (add to `features.implemented`)
   - Feature status changed (move between arrays)
   - New API endpoints added (update counts)
   - Tech stack changed

7. Update `docs/memory/roadmap.md` if:
   - Task completed (mark with ✅ and timestamp)
   - New tasks discovered (add to appropriate phase)

8. Update SMARTS pipeline diagram (if SMARTS agents changed):
   ```bash
   python scripts/update_smarts_diagram.py
   ```
   - This auto-updates the Miro board with current architecture
   - Requires MIRO_ACCESS_TOKEN and MIRO_BOARD_ID environment variables
   - Use `--dry-run` to test without updating Miro

## Format for Changelog Entry

```markdown
### YYYY-MM-DD HH:MM:SS
🔧 **Brief Title**
- What was changed
- Why it was changed
- Key files: `path/to/file.py`, `another/file.vue`
```

## Principle

Make MINIMAL necessary documentation changes only. Don't add unnecessary detail.
