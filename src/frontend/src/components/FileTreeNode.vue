<script>
import { defineComponent, h } from 'vue'

// Helper function for file sizes
const formatFileSize = (bytes) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
}

export default defineComponent({
  name: 'FileTreeNode',
  props: {
    item: Object,
    depth: Number,
    searchQuery: String,
    expandedFolders: Set
  },
  emits: ['toggle-folder', 'download'],
  setup(props, { emit }) {
    const isExpanded = () => props.expandedFolders.has(props.item.path)
    const indent = props.depth * 20

    const toggleFolder = () => {
      emit('toggle-folder', props.item.path)
    }

    const downloadFile = () => {
      emit('download', props.item.path, props.item.name)
    }

    return () => {
      if (props.item.type === 'directory') {
        return h('div', [
          // Folder row
          h('div', {
            class: 'flex items-center py-1.5 px-2 hover:bg-gray-50 dark:hover:bg-gray-700 rounded cursor-pointer group',
            style: { paddingLeft: `${indent}px` },
            onClick: toggleFolder
          }, [
            // Expand/collapse icon
            h('svg', {
              class: `w-4 h-4 mr-1 text-gray-500 dark:text-gray-400 transition-transform ${isExpanded() ? 'rotate-90' : ''}`,
              fill: 'currentColor',
              viewBox: '0 0 20 20'
            }, [
              h('path', {
                'fill-rule': 'evenodd',
                d: 'M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z',
                'clip-rule': 'evenodd'
              })
            ]),
            // Folder icon
            h('svg', {
              class: `w-5 h-5 mr-2 ${isExpanded() ? 'text-action-primary-500 dark:text-action-primary-400' : 'text-gray-500 dark:text-gray-400'}`,
              fill: 'currentColor',
              viewBox: '0 0 20 20'
            }, [
              h('path', {
                d: 'M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z'
              })
            ]),
            // Folder name
            h('span', {
              class: 'font-medium text-gray-700 dark:text-gray-200 flex-1'
            }, props.item.name),
            // File count badge
            h('span', {
              class: 'text-xs text-gray-500 dark:text-gray-400 mr-2'
            }, `${props.item.file_count}`)
          ]),
          // Children (when expanded)
          isExpanded() && props.item.children && props.item.children.length > 0
            ? h('div', props.item.children.map(child =>
                h('FileTreeNode', {
                  key: child.path,
                  item: child,
                  depth: props.depth + 1,
                  searchQuery: props.searchQuery,
                  expandedFolders: props.expandedFolders,
                  onToggleFolder: (path) => emit('toggle-folder', path),
                  onDownload: (path, name) => emit('download', path, name)
                })
              ))
            : null
        ])
      } else {
        // File row
        return h('div', {
          class: 'flex items-center py-1.5 px-2 hover:bg-gray-50 dark:hover:bg-gray-700 rounded group',
          style: { paddingLeft: `${indent + 20}px` }
        }, [
          // File icon
          h('svg', {
            class: 'w-4 h-4 mr-2 text-gray-500 dark:text-gray-400',
            fill: 'currentColor',
            viewBox: '0 0 20 20'
          }, [
            h('path', {
              'fill-rule': 'evenodd',
              d: 'M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z',
              'clip-rule': 'evenodd'
            })
          ]),
          // File name
          h('span', {
            class: 'text-gray-700 dark:text-gray-200 flex-1 truncate'
          }, props.item.name),
          // File size
          h('span', {
            class: 'text-xs text-gray-500 dark:text-gray-400 mr-4'
          }, formatFileSize(props.item.size)),
          // Download button
          h('button', {
            class: 'p-1 text-action-primary-600 dark:text-action-primary-400 opacity-0 group-hover:opacity-100 transition-opacity hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 rounded',
            title: 'Download file',
            onClick: downloadFile
          }, [
            h('svg', {
              class: 'w-4 h-4',
              fill: 'none',
              stroke: 'currentColor',
              viewBox: '0 0 24 24'
            }, [
              h('path', {
                'stroke-linecap': 'round',
                'stroke-linejoin': 'round',
                'stroke-width': '2',
                d: 'M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z'
              })
            ])
          ])
        ])
      }
    }
  }
})
</script>
