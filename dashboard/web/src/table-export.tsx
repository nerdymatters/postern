import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { hold, release } from './blobs'

export interface ExportTable { headers: string[]; rows: string[][] }

/** Spreadsheets run a cell that starts like a formula, and names here come from a Sheet and from Google
 *  accounts: a leading apostrophe keeps such a cell text. Quoting every cell stops a separator in any
 *  locale's spreadsheet from starting a new one. */
function csvCell(value: string) {
  const text = /^[\s\p{Cc}]*[=+\-@＝＋－＠]/u.test(value) ? `'${value}` : value
  return `"${text.replaceAll('"', '""')}"`
}

/** Opens in Excel, Numbers and Google Sheets with Thai names intact: UTF-8 with a byte-order mark, and CRLF line ends. */
export function csv({ headers, rows }: ExportTable) {
  return `\uFEFF${[headers, ...rows].map(row => row.map(csvCell).join(',')).join('\r\n')}\r\n`
}

export function downloadCsv(name: string, table: ExportTable) {
  const url = hold(new Blob([csv(table)], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${name}.csv`
  document.body.append(link)
  link.click()
  link.remove()
  // The browser reads the file after this click returns, sometimes much later on a phone.
  setTimeout(() => release(url), 60_000)
}

/** What the browser prints: this report alone (index.css hides everything else). The page rule lives here,
 *  so printing any other screen keeps the browser's own settings. Unlike a certificate it keeps a page margin,
 *  which is where a browser can add page numbers. While mounted, the page title names a saved PDF: it carries
 *  no person's name, as a shared tablet keeps its history. */
export function PrintableReport({ title, heading, notes, table }: { title: string; heading: string; notes: string[]; table: ExportTable }) {
  useEffect(() => {
    const before = document.title
    document.title = title
    return () => { document.title = before }
  }, [title])
  return createPortal(<div className="print-report">
    <style>{'@page { size: landscape; margin: 0.5in; }'}</style>
    <h1 className="text-lg font-semibold">{heading}</h1>
    {notes.map(note => <p key={note} className="mt-1 text-xs">{note}</p>)}
    <table className="mt-3 w-full border-collapse text-left text-xs">
      <thead><tr className="border-b-2 border-black">{table.headers.map(header => <th key={header} scope="col" className="px-1 py-1.5 align-bottom font-semibold">{header}</th>)}</tr></thead>
      <tbody>{table.rows.map((row, index) => <tr key={index} className="border-b border-neutral-300">{row.map((cell, column) => <td key={column} className="px-1 py-1 align-top">{cell}</td>)}</tr>)}</tbody>
    </table>
  </div>, document.body)
}
