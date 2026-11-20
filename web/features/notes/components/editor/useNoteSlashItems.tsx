'use client'

import { useCallback, type MutableRefObject, type RefObject } from 'react'
import type { PartialBlock } from '@blocknote/core'
import { SuggestionMenu } from '@blocknote/core/extensions'
import { getDefaultReactSlashMenuItems } from '@blocknote/react'
import {
  BookOpen,
  Bookmark,
  CalendarDays,
  Columns2,
  FileCode,
  FileText,
  Film,
  Globe,
  Heading1,
  Heading2,
  Heading3,
  Image as ImageIcon,
  Info,
  ListTree,
  Music,
  Sigma,
  Sparkles,
  Table2,
  Upload,
} from 'lucide-react'

import { listNotes } from '@/lib/notes-api'
import {
  basicPreviewKinds,
  isBasicPreviewKind,
  type BlockPreviewKind,
} from '../../model/block-previews'
import { LEARNING_KINDS } from '../../model/learning-kinds'
import { noteMediaAccept } from '../../model/media'
import type { NoteMenuItem } from '../NoteInsertMenu'
import type { NotesEditor, schema } from './schema'

const SHORTCUTS: Record<string, string> = {
  heading: '#',
  heading_2: '##',
  heading_3: '###',
  heading_4: '####',
  bullet_list: '-',
  numbered_list: '1.',
  check_list: '[]',
  toggle_list: '>',
  quote: '"',
  code_block: '```',
  divider: '---',
}

interface UseNoteSlashItemsOptions {
  editor: NotesEditor
  pageId: string
  containerRef: RefObject<HTMLDivElement | null>
  uploadInputRef: RefObject<HTMLInputElement | null>
  uploadAnchorRef: MutableRefObject<string | null>
  uploadRetryRef: MutableRefObject<string | null>
  onAI: (instruction: string) => void
}

export default function useNoteSlashItems({
  editor,
  pageId,
  containerRef,
  uploadInputRef,
  uploadAnchorRef,
  uploadRetryRef,
  onAI,
}: UseNoteSlashItemsOptions) {
  

  const getItems = useCallback(
    async (query: string): Promise<NoteMenuItem[]> => {
      const insert = (block: Record<string, unknown>) => {
        const current = editor.getTextCursorPosition().block
        const empty =
          current.type === 'paragraph' && (!current.content || current.content.length === 0)
        const [created] = empty
          ? editor.replaceBlocks(
              [current],
              [
                block as PartialBlock<
                  typeof schema.blockSchema,
                  typeof schema.inlineContentSchema,
                  typeof schema.styleSchema
                >,
              ]
            ).insertedBlocks
          : editor.insertBlocks(
              [
                block as PartialBlock<
                  typeof schema.blockSchema,
                  typeof schema.inlineContentSchema,
                  typeof schema.styleSchema
                >,
              ],
              current,
              'after'
            )
        if (created) {
          // Form blocks own focus. Focusing the editor here immediately blurs an
          // auto-focused formula input and closes it before the user can type.
          if (['math', 'webCard', 'htmlEmbed', 'learning'].includes(created.type)) {
            // SuggestionMenu restores editor focus after the insertion callback.
            // Hand it to the new form after that transaction and React commit.
            requestAnimationFrame(() => {
              containerRef.current
                ?.querySelector<HTMLElement>(
                  `[data-id="${CSS.escape(created.id)}"] input, [data-id="${CSS.escape(created.id)}"] textarea`
                )
                ?.focus()
            })
          } else {
            const target =
              created.type === 'noteColumns'
                ? created.children?.[0]?.children?.[0]?.id || created.id
                : created.id
            editor.setTextCursorPosition(target, 'start')
            editor.focus()
          }
        }
        return created
      }

      const item = (
        title: string,
        group: string,
        subtext: string,
        aliases: string[],
        Icon: typeof Info,
        action: () => void,
        preview: BlockPreviewKind,
        shortcut?: string
      ): NoteMenuItem => ({
        title: (title),
        group: (group),
        subtext: (subtext),
        aliases,
        icon: <Icon size={19} strokeWidth={1.6} />,
        onItemClick: action,
        preview,
        shortcut,
      })

      const defaults = getDefaultReactSlashMenuItems(editor).flatMap(i => {
        const key = (i as { key?: string }).key || ''
        return isBasicPreviewKind(key)
          ? [{ ...i, preview: key, group: "Basic blocks", shortcut: SHORTCUTS[key] }]
          : []
      })
      defaults.sort(
        (a, b) => basicPreviewKinds.indexOf(a.preview) - basicPreviewKinds.indexOf(b.preview)
      )

      const items: NoteMenuItem[] = [
        item(
          'Ask Dito',
          'Suggested',
          'Write, explain or improve this part of your note.',
          ['ai', '生成', '续写'],
          Sparkles,
          () => onAI(''),
          'ai'
        ),
        item(
          'Reference a source',
          'Suggested',
          'Bring in a passage from your reading, chats or knowledge bases.',
          ['source', '引用', '知识'],
          BookOpen,
          () =>
            containerRef.current?.dispatchEvent(new CustomEvent('note-sources', { bubbles: true })),
          'source'
        ),
        item(
          'Insert favorite',
          'Suggested',
          'Keep an original answer with its source.',
          ['favorite', '收藏'],
          Bookmark,
          () =>
            containerRef.current?.dispatchEvent(
              new CustomEvent('note-favorites', { bubbles: true })
            ),
          'favorite'
        ),
        ...defaults,
        item(
          'Callout',
          'Basic blocks',
          'Highlight a key idea, tip or warning.',
          ['callout', '标注', '提示'],
          Info,
          () => insert({ type: 'callout' }),
          'callout'
        ),
        item(
          'Page link',
          'Basic blocks',
          'Link to another notebook page.',
          ['page', '页面', '链接'],
          FileText,
          () => editor.getExtension(SuggestionMenu)?.openSuggestionMenu('[['),
          'pageLink'
        ),
        ...(
          [
            ['image', 'Image', ImageIcon, 'image/png,image/jpeg,image/gif,image/webp', '图片'],
            ['video', 'Video', Film, 'video/mp4', '视频'],
            ['audio', 'Audio', Music, 'audio/mpeg,audio/wav', '音频'],
            ['file', 'File', Upload, noteMediaAccept, '文件'],
          ] as const
        ).map(([key, label, Icon, accept, alias]) =>
          item(
            label,
            'Media',
            'Upload a file to this page.',
            [key, alias],
            Icon,
            () => {
              if (uploadInputRef.current) {
                uploadAnchorRef.current = editor.getTextCursorPosition().block.id
                uploadRetryRef.current = null
                uploadInputRef.current.accept = accept
                uploadInputRef.current.click()
              }
            },
            key
          )
        ),
        item(
          'Web bookmark',
          'Media',
          'Save a link with a title and a short description.',
          ['bookmark', '书签'],
          Bookmark,
          () => insert({ type: 'webCard' }),
          'bookmark'
        ),
        item(
          'Embed',
          'Media',
          'Display an external page that allows embedding.',
          ['embed', '嵌入'],
          Globe,
          () => insert({ type: 'webCard', props: { mode: 'embed' } }),
          'embed'
        ),
        item(
          'HTML',
          'Media',
          'Preview HTML inside an isolated frame.',
          ['html', '网页'],
          FileCode,
          () => insert({ type: 'htmlEmbed' }),
          'htmlEmbed'
        ),
        ...(['table', 'board', 'list'] as const).map(view =>
          item(
            { table: 'Task table', board: 'Task board', list: 'Task list' }[view],
            'Linked views',
            'View and update tasks from this workspace.',
            ['task', view, '任务', { table: '表格', board: '看板', list: '列表' }[view]],
            Table2,
            () => insert({ type: 'taskView', props: { view } }),
            `task_${view}`
          )
        ),
        item(
          'Table of contents',
          'Advanced blocks',
          'Jump to headings; updates as your page changes.',
          ['toc', '目录'],
          ListTree,
          () => insert({ type: 'tableOfContents' }),
          'toc'
        ),
        item(
          'Math formula',
          'Advanced blocks',
          'Write a mathematical expression using LaTeX.',
          ['math', '公式'],
          Sigma,
          () => insert({ type: 'math' }),
          'math',
          '$$'
        ),
        ...([1, 2, 3] as const).map(level =>
          item(
            `Toggle heading ${level}`,
            'Advanced blocks',
            'Keep supporting notes inside a collapsible heading.',
            ['toggle', '折叠', `h${level}`],
            [Heading1, Heading2, Heading3][level - 1],
            () =>
              insert({ type: 'toggleHeading', props: { level }, children: [{ type: 'paragraph' }] }),
            `toggle_heading_${level}`
          )
        ),
        ...([2, 3, 4, 5] as const).map(count =>
          item(
            `${count} columns`,
            'Advanced blocks',
            'Arrange related ideas side by side. Stacks on small screens.',
            ['columns', '列', String(count)],
            Columns2,
            () =>
              insert({
                type: 'noteColumns',
                children: Array.from({ length: count }, () => ({
                  type: 'noteColumn',
                  children: [{ type: 'paragraph' }],
                })),
              }),
            `columns_${count}`
          )
        ),
        item(
          'Date',
          'Inline',
          'Add an editable date in your text.',
          ['date', '日期'],
          CalendarDays,
          () =>
            editor.insertInlineContent([
              { type: 'noteDate', props: { date: new Date().toLocaleDateString('en-CA') } },
              ' ',
            ]),
          'date'
        ),
        item(
          'Inline formula',
          'Inline',
          'Place a formula within a sentence.',
          ['inline math', '行内公式'],
          Sigma,
          () => editor.insertInlineContent([{ type: 'inlineMath', props: { formula: 'x^2' } }, ' ']),
          'inlineMath',
          '$…$'
        ),
        ...LEARNING_KINDS.map(({ kind, label, alias, Icon }) =>
          item(
            label,
            'Learning',
            'Create learning material grounded in this page.',
            [kind, alias],
            Icon,
            () => {
              insert({ type: 'learning', props: { kind } })
            },
            ({ image: 'generated_image', audio: 'podcast' }[kind] || kind) as BlockPreviewKind
          )
        ),
      ]

      const needle = query.toLowerCase().trim()
      return items.filter(i =>
        [i.title, ...(i.aliases || [])].some(label => label.toLowerCase().includes(needle))
      )
    },
    [containerRef, editor, onAI, uploadAnchorRef, uploadInputRef, uploadRetryRef]
  )

  const pageItems = useCallback(
    async (query: string): Promise<NoteMenuItem[]> => {
      const { documents } = await listNotes()
      return documents
        .filter(d => d.id !== pageId && d.title.toLowerCase().includes(query.toLowerCase()))
        .slice(0, 15)
        .map(d => ({
          title: d.title,
          preview: 'pageLink' as const,
          previewTitle: d.title,
          subtext: "Open linked page",
          icon: <FileText size={19} />,
          onItemClick: () =>
            editor.insertInlineContent([
              { type: 'pageLink', props: { pageId: d.id, title: d.title } },
              ' ',
            ]),
        }))
    },
    [editor, pageId]
  )

  return { getItems, pageItems }
}
