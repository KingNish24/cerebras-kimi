'use client'

import { useState } from 'react'
import dynamic from '@/shared/router/dynamic'
import {
  BlockNoteEditor,
  BlockNoteSchema,
  createExtension,
  defaultBlockSpecs,
  defaultInlineContentSpecs,
} from '@blocknote/core'
import { createReactBlockSpec, createReactInlineContentSpec } from '@blocknote/react'
import { InputRule, inputRules } from '@tiptap/pm/inputrules'
import { ArrowUpRight, FileText } from 'lucide-react'

import type { NoteBlock } from '@/lib/notes-api'
import { parseNoteMarkdown } from '../../model/markdown'
import NoteFavoriteBlock from '../NoteFavoriteBlock'
import { noteFile } from '../NoteFile'
import NoteMath from '../NoteMath'
import {
  callout,
  htmlEmbed,
  noteColumn,
  noteColumns,
  noteDate,
  tableOfContents,
  taskView,
  toggleHeading,
  webCard,
} from '../NoteRichBlocks'
import { noteUpload } from '../NoteUpload'

const Markdown = dynamic(() => import('@/components/common/MarkdownRenderer'), { ssr: false })
const LearningContent = dynamic(() => import('../NoteLearningContent'), { ssr: false })

const math = createReactBlockSpec(
  { type: 'math', propSchema: { formula: { default: '' } }, content: 'none' },
  {
    render: function MathBlock({ block, editor }) {
      
      const [editing, setEditing] = useState(!block.props.formula)
      return (
        <div className="dt-note-math" contentEditable={false}>
          {editing ? (
            <input
              autoFocus
              aria-label={"Math formula"}
              value={block.props.formula}
              onChange={e => editor.updateBlock(block, { props: { formula: e.target.value } })}
              onBlur={() => {
                if (block.props.formula.trim()) setEditing(false)
              }}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === 'Escape') {
                  e.preventDefault()
                  setEditing(false)
                  editor.focus()
                }
              }}
            />
          ) : (
            <button onClick={() => setEditing(true)}>
              <NoteMath formula={block.props.formula} display />
            </button>
          )}
        </div>
      )
    },
  }
)

const clip = createReactBlockSpec(
  {
    type: 'clip',
    content: 'none',
    propSchema: {
      title: { default: '' },
      markdown: { default: '' },
      sourceUrl: { default: '' },
      sourceId: { default: '' },
      favoriteType: { default: '' },
      summary: { default: '' },
      metadata: { default: '{}' },
      expanded: { default: false },
    },
  },
  {
    render: function ClipBlock({ block, editor }) {
      
      if (block.props.favoriteType || /^\/notebooks?\//.test(block.props.sourceUrl))
        return (
          <NoteFavoriteBlock
            title={block.props.title}
            markdown={block.props.markdown}
            sourceUrl={block.props.sourceUrl}
            type={block.props.favoriteType}
            summary={block.props.summary}
            metadata={block.props.metadata}
            expanded={block.props.expanded}
            onActivate={() => {
              if (editor.getTextCursorPosition().block.id !== block.id)
                editor.setTextCursorPosition(block.id, 'start')
            }}
            onExpanded={expanded =>
              editor.transact(tr => {
                // Reading a saved excerpt is view state, not a content edit.
                tr.setMeta('addToHistory', false)
                editor.updateBlock(block, { props: { expanded } })
              })
            }
          />
        )
      return (
        <div className="dt-note-clip" contentEditable={false}>
          <details open>
            <summary>{block.props.title || "Excerpt"}</summary>
            <Markdown content={block.props.markdown} />
          </details>
          {block.props.sourceUrl && (
            <a data-note-source href={block.props.sourceUrl}>
              {"Return to source"}
            </a>
          )}
        </div>
      )
    },
  }
)

const learning = createReactBlockSpec(
  {
    type: 'learning',
    content: 'none',
    propSchema: {
      kind: { default: 'mermaid' },
      prompt: { default: '' },
      markdown: { default: '' },
      data: { default: '{}' },
      sourceUrl: { default: '' },
    },
  },
  {
    render: ({ block, editor }) => (
      <LearningContent
        id={block.id}
        {...block.props}
        onChange={props => editor.updateBlock(block, { props })}
      />
    ),
  }
)

const pageLink = createReactInlineContentSpec(
  {
    type: 'pageLink',
    propSchema: { pageId: { default: '' }, title: { default: '' } },
    content: 'none',
  },
  {
    render: ({ inlineContent }) => (
      <a className="dt-note-link" href={`/co-writer/${inlineContent.props.pageId}`}>
        <FileText size={14} aria-hidden="true" />
        <span>{inlineContent.props.title}</span>
        <ArrowUpRight size={12} aria-hidden="true" />
      </a>
    ),
  }
)

const inlineMath = createReactInlineContentSpec(
  { type: 'inlineMath', propSchema: { formula: { default: '' } }, content: 'none' },
  {
    render: function InlineMath({ inlineContent, updateInlineContent }) {
      
      const [edit, setEdit] = useState(false)
      return (
        <span contentEditable={false} className="dt-note-inline-math">
          {edit ? (
            <input
              aria-label={"Math formula"}
              autoFocus
              value={inlineContent.props.formula}
              onChange={e =>
                updateInlineContent({ type: 'inlineMath', props: { formula: e.target.value } })
              }
              onBlur={() => setEdit(false)}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === 'Escape') {
                  e.preventDefault()
                  setEdit(false)
                }
              }}
            />
          ) : (
            <button onClick={() => setEdit(true)}>
              <NoteMath formula={inlineContent.props.formula} />
            </button>
          )}
        </span>
      )
    },
  }
)

export const noteInputRules = createExtension({
  key: 'note-input-rules',
  inputRules: [{ find: /^\$\$\s$/, replace: () => ({ type: 'math', props: { formula: '' } }) }],
  prosemirrorPlugins: [
    inputRules({
      rules: [
        new InputRule(/(?:^|[^$])\$([^$\n]+)\$$/, (state, match, start, end) => {
          const node = state.schema.nodes.inlineMath
          if (!node) return null
          const offset = match[0].startsWith('$') ? 0 : 1
          return state.tr.replaceWith(start + offset, end, node.create({ formula: match[1] }))
        }),
      ],
    }),
  ],
})

export const schema = BlockNoteSchema.create({
  blockSpecs: {
    ...defaultBlockSpecs,
    file: noteFile(),
    math: math(),
    clip: clip(),
    learning: learning(),
    noteUpload: noteUpload(),
    callout: callout(),
    toggleHeading: toggleHeading(),
    noteColumn: noteColumn(),
    noteColumns: noteColumns(),
    tableOfContents: tableOfContents(),
    webCard: webCard(),
    htmlEmbed: htmlEmbed(),
    taskView: taskView(),
  },
  inlineContentSpecs: { ...defaultInlineContentSpecs, pageLink, inlineMath, noteDate },
})

export type NotesEditor = typeof schema.BlockNoteEditor

/** Drop inline content a block type cannot hold. A stray string there (a model
 *  echoing "timeline" into a learning block) makes the editor reject the page. */
export function fitSchema(blocks: NoteBlock[]): NoteBlock[] {
  return blocks.map(block => {
    const spec = (schema.blockSchema as Record<string, { content?: string } | undefined>)[
      block.type
    ]
    const { content, ...rest } = block
    return {
      ...(spec?.content === 'none' ? rest : { ...rest, content }),
      children: fitSchema(block.children || []),
    } as NoteBlock
  })
}

/** Reuse the notebook's normal migration before clipping into an unopened legacy page. */
export function parseLegacyNoteContent(markdown: string): NoteBlock[] {
  const editor = BlockNoteEditor.create({ schema })
  try {
    return parseNoteMarkdown(markdown, text => editor.tryParseMarkdownToBlocks(text) as NoteBlock[])
  } finally {
    editor._tiptapEditor.destroy()
  }
}
