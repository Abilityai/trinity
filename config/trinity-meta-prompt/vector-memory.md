# Trinity Vector Memory

You have a **Chroma MCP server** configured for semantic memory storage and retrieval.

## Quick Reference

| Item | Value |
|------|-------|
| **MCP Server** | `chroma` (use `mcp__chroma__*` tools) |
| **Data Directory** | `/home/developer/vector-store/` |
| **Persistence** | SQLite-backed (survives restarts) |

## Available Tools

Use these MCP tools for vector operations:

### Collection Management
- `chroma_list_collections` - List all collections
- `chroma_create_collection` - Create a new collection
- `chroma_get_collection_info` - Get collection metadata
- `chroma_get_collection_count` - Get document count
- `chroma_delete_collection` - Delete a collection

### Document Operations
- `chroma_add_documents` - Add documents with auto-generated embeddings
- `chroma_query_documents` - Search by semantic similarity
- `chroma_get_documents` - Retrieve by ID or metadata filter
- `chroma_update_documents` - Update existing documents
- `chroma_delete_documents` - Delete documents

## Usage Examples

### Store a Memory
```
mcp__chroma__chroma_add_documents(
    collection_name="memory",
    documents=["User prefers Python over JavaScript"],
    ids=["pref-001"],
    metadatas=[{"type": "preference", "source": "user"}]
)
```

### Query by Similarity
```
mcp__chroma__chroma_query_documents(
    collection_name="memory",
    query_texts=["What programming languages does the user like?"],
    n_results=5
)
```

### Query with Metadata Filter
```
mcp__chroma__chroma_query_documents(
    collection_name="memory",
    query_texts=["authentication"],
    n_results=5,
    where={"type": "technical"}
)
```

## Best Practices

1. **Use descriptive IDs**: `pref-{topic}-001`, `task-{id}`, `error-{hash}`
2. **Add metadata**: Include `type`, `timestamp`, `source` for filtering
3. **Query before solving**: Check if you've encountered similar problems before
4. **Use multiple collections**: Organize by domain (preferences, tasks, errors)

## Resources

- [Chroma MCP Server](https://github.com/chroma-core/chroma-mcp)
- [Chroma Documentation](https://docs.trychroma.com/)
