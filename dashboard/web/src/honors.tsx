import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { api, currentToken, inline, type Certificate, type QuizStanding } from './api'
import { dated, EASTERN } from './format'
import { useBranding } from './branding'
import { Link } from './routes'
import { Button, buttonClass, input, Label, Message, Thai } from './ui'
import { IconArrowRight, IconAward, IconCheck, IconPrint, IconQuiz, IconRename, IconRetake } from './PosternIcons'

/** The longest printed name the server saves (postern/honors.py NAME_LIMIT). */
export const NAME_LIMIT = 80

// Match honors.tidy(): Python counts Unicode code points after cleaning, not UTF-16 input units.
const NAME_INVISIBLE = /[\u115f\u1160\u3164\uffa0\u2800\u034f\u17b4\u17b5\u180b\u180c\u180d\u180f\u{13441}\u{13442}\ufe00-\ufe0f\u{e0100}-\u{e01ef}]/u
const NAME_SPACE = /[\p{White_Space}\u001c-\u001f]/u
const NAME_JOINERS = new Set(['\u200c', '\u200d'])
const NAME_NEIGHBOR = /[\p{L}\p{M}]/u

export function tidyCertificateName(value: string): string {
  const kept: string[] = []
  let marks = 0
  for (const c of value.normalize('NFC')) {
    if (NAME_INVISIBLE.test(c) || (/\p{C}/u.test(c) && !NAME_SPACE.test(c) && !NAME_JOINERS.has(c))) continue
    // Ahom medial ra became a spacing mark in Unicode 16; match the server even on older browsers.
    if (!NAME_JOINERS.has(c)) marks = /[\p{Mn}\p{Me}]/u.test(c) && c !== '\u{1171e}' ? marks + 1 : 0
    if (NAME_JOINERS.has(c) || marks <= 3) kept.push(c)
  }
  return kept.filter((c, i) => !NAME_JOINERS.has(c) || (i > 0 && i < kept.length - 1 && NAME_NEIGHBOR.test(kept[i - 1]) && NAME_NEIGHBOR.test(kept[i + 1])))
    .join('').replace(/[\p{White_Space}\u001c-\u001f]+/gu, ' ').trim().normalize('NFC')
}

const legibleName = (name: string) => /[\p{L}\p{N}]/u.test(name)

/** One badge on the shelf. Every one is a pass the server already records: badges keep no state of their own. */
export interface Badge {
  key: string
  kind: 'chapter' | 'chapters' | 'final'
  chapter?: number
  title: string; th: string
  subtitle: string; subtitleTh?: string
  earned: boolean; earnedAt: number | null
  hint: string; hintTh: string
}

/** "SOP Chapter 3: Professional Appearance…" reads "Professional Appearance…" under its Chapter 3 badge. Like
 *  postern/quiz.py chapter_number(), it reads past full-width digits and zero-width characters pasted from Docs or LINE. */
export function chapterTitle(category: string) {
  const name = category.normalize('NFKC').replace(/\p{Cf}/gu, '')
  const title = name.replace(/^\s*(?:sop\s*[:-]?\s*)?(?:chapter|บทที่)\s*[.:#-]?\s*[0-9๐-๙]+\s*[:.\-–—]?\s*/i, '')
  return title === name ? category : title.trim() || category
}

/** A badge for each chapter quiz passed, one for passing them all, and one for the Final Exam, which brings the
 *  certificate. A pass is permanent and retakes never undo it; like its quiz, a chapter's badge shows while the
 *  chapter is published, and All chapters counts the chapters published now. */
export function badges(quizzes: QuizStanding[], finalExam?: QuizStanding | null, certificate?: Certificate | null): Badge[] {
  const chapters = [...quizzes].sort((a, b) => a.chapter - b.chapter)
  const list: Badge[] = chapters.map(quiz => ({
    kind: 'chapter', key: `chapter-${quiz.chapter}`, chapter: quiz.chapter,
    title: `Chapter ${quiz.chapter}`, th: `บทที่ ${quiz.chapter}`, subtitle: chapterTitle(quiz.category),
    earned: quiz.passed, earnedAt: quiz.passed ? quiz.passed_at : null,
    hint: 'Pass its quiz to earn it.', hintTh: 'สอบผ่านแบบทดสอบท้ายบทเพื่อรับเหรียญนี้',
  }))
  if (chapters.length > 1) {
    const all = chapters.every(quiz => quiz.passed)
    list.push({
      kind: 'chapters', key: 'chapters', title: 'All chapters', th: 'ครบทุกบท',
      subtitle: `All ${chapters.length} SOP chapter quizzes`, subtitleTh: `แบบทดสอบท้ายบท SOP ครบทั้ง ${chapters.length} บท`,
      earned: all, earnedAt: all ? Math.max(...chapters.map(quiz => quiz.passed_at ?? 0)) || null : null,
      hint: 'Pass every chapter quiz to earn it.', hintTh: 'สอบผ่านครบทุกบทเพื่อรับเหรียญนี้',
    })
  }
  if (finalExam || certificate) {
    const earned = !!certificate || !!finalExam?.passed
    list.push({
      kind: 'final', key: 'final', title: 'Final Exam', th: 'แบบทดสอบปลายหลักสูตร',
      subtitle: 'With your certificate', subtitleTh: 'พร้อมประกาศนียบัตร',
      earned, earnedAt: certificate?.awarded_at ?? (finalExam?.passed ? finalExam.passed_at : null),
      hint: 'Pass the Final Exam to earn it.', hintTh: 'สอบผ่านแบบทดสอบปลายหลักสูตรเพื่อรับเหรียญนี้',
    })
  }
  return list
}

/** Points alternating between two radii: the serrated edge of a rosette or a seal. */
function rosette(cx: number, cy: number, outer: number, inner: number, points: number) {
  return Array.from({ length: points * 2 }, (_, index) => {
    const radius = index % 2 ? inner : outer, angle = index * Math.PI / points - Math.PI / 2
    return `${index ? 'L' : 'M'}${(cx + radius * Math.cos(angle)).toFixed(2)} ${(cy + radius * Math.sin(angle)).toFixed(2)}`
  }).join('') + 'Z'
}
const MEDAL_EDGE = rosette(32, 30, 28, 25, 20)
const SEAL_EDGE = rosette(60, 60, 58, 53.5, 36)

/** Badge faces, drawn on the 24 px icon grid like PosternIcons. The chapter ones follow the topics of the
 *  bundled SOP chapters, which quizzes.json numbers the same way; any other chapter gets the plain award. */
const AWARD = <><circle cx="12" cy="9" r="6" /><path d="M8.5 13.9 7 21.5l5-2.8 5 2.8-1.5-7.6" /></>
const GLYPHS: Record<string, ReactNode> = {
  // 1 Introduction: the way in. 2 Service excellence. 3 Appearance and grooming. 4 Service flow and communication.
  'chapter-1': <path d="M7 21V11a5 5 0 0 1 10 0v10M4 21h16M14 15.5h.01" />,
  'chapter-2': <path d="M12 2.5 14.47 8.6 21.03 9.06 15.99 13.3 17.58 19.69 12 16.2 6.42 19.69 8.01 13.3 2.97 9.06 9.53 8.6z" />,
  'chapter-3': <path d="M8 4 3 7l2 4 3-1v10h8V10l3 1 2-4-5-3M8 4l4 4 4-4" />,
  'chapter-4': <path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-7l-5 4v-4H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM8 11h.01M12 11h.01M16 11h.01" />,
  // 5 Customer handling. 6 Complaint handling. 7 Cleanliness and safety. 8 Roles by position. 9 Monitoring and improvement.
  'chapter-5': <path d="M12 20C6 16 3 12.5 3 9a4.5 4.5 0 0 1 9-1 4.5 4.5 0 0 1 9 1c0 3.5-3 7-9 11z" />,
  'chapter-6': <path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-7l-5 4v-4H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zM8.5 11l2.5 2.5 4.5-4.5" />,
  'chapter-7': <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6zM9 12l2 2 4-4" />,
  'chapter-8': <><circle cx="9" cy="8" r="3.5" /><path d="M3 20.5a6 6 0 0 1 12 0M15.5 4.6a3.5 3.5 0 0 1 0 6.8M18.2 14.6a6 6 0 0 1 3.3 5.9" /></>,
  'chapter-9': <path d="M3 17l6-6 4 4 8-8M15 7h6v6" />,
  // Every chapter: a trophy. The Final Exam: a mortarboard.
  chapters: <path d="M7 4h10v6a5 5 0 0 1-10 0zM7 6H4v2a3 3 0 0 0 3 3M17 6h3v2a3 3 0 0 1-3 3M12 15v3M8 21h8l-1-3H9z" />,
  final: <path d="M2 9l10-5 10 5-10 5zM6 11v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5M22 9v6" />,
}

function Gold({ id }: { id: string }) {
  return <linearGradient id={id} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#fde68a" /><stop offset=".5" stopColor="#f59e0b" /><stop offset="1" stopColor="#b45309" /></linearGradient>
}

const MEDAL_SIZES = { sm: 'h-12 w-10', md: 'h-[4.75rem] w-16', lg: 'h-24 w-20' }

/** A rosette with ribbons: gold and green once earned, a pale outline until then. */
export function Medal({ badge, size = 'lg', className = '' }: { badge: Badge; size?: keyof typeof MEDAL_SIZES; className?: string }) {
  const gold = `medal${useId().replace(/[^\w-]/g, '')}`
  const label = `${badge.title} badge: ${badge.earned ? `earned${badge.earnedAt ? ` ${on(badge.earnedAt)}` : ''}` : 'not earned yet'}`
  return <svg viewBox="0 0 64 76" role="img" aria-label={label} className={`${MEDAL_SIZES[size]} shrink-0 ${className}`}>
    <title>{label}</title>
    {badge.earned ? <defs><Gold id={gold} /></defs> : null}
    <path d="M22 44 14 73l7.5-4.5L27 75l5.5-23z" className={badge.earned ? 'fill-emerald-600' : 'fill-neutral-200 dark:fill-neutral-700'} />
    <path d="M42 44l8 29-7.5-4.5L37 75l-5.5-23z" className={badge.earned ? 'fill-emerald-800' : 'fill-neutral-300 dark:fill-neutral-600'} />
    <path d={MEDAL_EDGE} fill={badge.earned ? `url(#${gold})` : undefined} className={badge.earned ? undefined : 'fill-neutral-200 dark:fill-neutral-700'} />
    <circle cx="32" cy="30" r="19.5" strokeWidth="1.5" className={badge.earned ? 'fill-white stroke-amber-600' : 'fill-neutral-50 stroke-neutral-300 dark:fill-neutral-900 dark:stroke-neutral-600'} />
    {badge.earned ? <path d="M10.86 22.3A22.5 22.5 0 0 1 24.3 8.86" fill="none" stroke="#fff" strokeOpacity=".65" strokeWidth="2" strokeLinecap="round" /> : null}
    <g transform="translate(18.2 16.2) scale(1.15)" fill="none" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      className={badge.earned ? 'stroke-emerald-700' : 'stroke-neutral-400 dark:stroke-neutral-500'}>{GLYPHS[badge.key] ?? AWARD}</g>
  </svg>
}

/** Badge and certificate dates: Eastern, like the year in the certificate number. */
const on = (seconds: number, month: 'short' | 'long' = 'short') => dated(seconds, month, EASTERN)

const CONFETTI = ['#10b981', '#f59e0b', '#38bdf8', '#f43f5e', '#a78bfa', '#facc15']

/** A short burst over a new badge (index.css); not shown to readers who prefer reduced motion. */
export function Confetti() {
  return <div aria-hidden="true" className="confetti pointer-events-none absolute inset-0 overflow-hidden">
    {Array.from({ length: 32 }, (_, index) => <span key={index} className="confetti-piece" style={{
      left: `${(index * 37 + 11) % 100}%`, background: CONFETTI[index % CONFETTI.length],
      '--delay': `${(index % 8) * 70}ms`, '--drift': `${(index * 53) % 90 - 45}px`, '--spin': `${(index * 97) % 540 + 180}deg`,
    } as CSSProperties} />)}
  </div>
}

/** The moment a first pass earns something: the new badge, or with the Final Exam the certificate. */
export function NewBadge({ badge, alsoAll = false }: { badge: Badge; alsoAll?: boolean }) {
  const final = badge.kind === 'final'
  return <div className="flex items-center gap-4 rounded-xl border border-amber-300 bg-white/90 p-4 text-neutral-900 dark:border-amber-700 dark:bg-neutral-900/80 dark:text-neutral-100">
    <Medal badge={badge} size="md" className="medal-pop" />
    <div className="min-w-0 space-y-1">
      <p className="font-semibold">{final ? 'You earned your certificate!' : `New badge: ${badge.title}`}<Thai tone="inherit" className="font-medium">{final ? 'คุณได้รับประกาศนียบัตรแล้ว!' : `ได้รับเหรียญตราใหม่: ${badge.th}`}</Thai></p>
      <p className="break-words text-sm text-neutral-600 dark:text-neutral-300">{final ? 'It carries your name, the date and a certificate number. Print it or save it as a PDF.' : badge.subtitle}
        {final ? <Thai className="mt-0.5">มีชื่อของคุณ วันที่ และเลขที่ประกาศนียบัตร พิมพ์หรือบันทึกเป็น PDF ได้</Thai> : null}</p>
      {alsoAll ? <p className="text-sm font-medium">You also earned the All chapters badge.<Thai>และยังได้รับเหรียญครบทุกบทด้วย</Thai></p> : null}
      {final ? null : <Link to="/badges" className="inline-flex items-center gap-1 text-sm font-medium underline">See your badges <span lang="th">· ดูเหรียญตราของคุณ</span><IconArrowRight className="size-4" /></Link>}
    </div>
  </div>
}

/** "Your badges" on the home page: the whole collection at a glance. The way to the certificate is in the
 *  training guide above it, where a finished reader looks first, and on the badges page. */
export function BadgeShelf({ quizzes, finalExam, certificate }: { quizzes: QuizStanding[]; finalExam?: QuizStanding | null; certificate?: Certificate | null }) {
  const list = badges(quizzes, finalExam, certificate)
  if (!list.length) return null
  const earned = list.filter(badge => badge.earned).length
  return <section aria-labelledby="badge-shelf" className="space-y-4 rounded-2xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
    <div className="flex items-start justify-between gap-3">
      <h2 id="badge-shelf" className="font-semibold">Your badges<Thai tone="inherit" className="font-medium">เหรียญตราของคุณ</Thai></h2>
      <p className="text-right text-sm"><span className="block font-medium tabular-nums">{earned} of {list.length} earned</span><Thai>ได้รับแล้ว {earned} จาก {list.length} เหรียญ</Thai></p>
    </div>
    <ul aria-label="Badges" className="flex flex-wrap gap-x-2 gap-y-3">{list.map(badge => <li key={badge.key}><Medal badge={badge} size="sm" /></li>)}</ul>
    {!earned ? <p className="text-sm text-neutral-600 dark:text-neutral-300">Pass a chapter quiz to earn your first badge.<Thai className="mt-1">สอบผ่านแบบทดสอบท้ายบทเพื่อรับเหรียญตราแรกของคุณ</Thai></p>
      : earned === list.length ? <p className="text-sm font-medium text-amber-900 dark:text-amber-200">You have earned every badge. Well done!<Thai tone="inherit" className="mt-1 font-normal">คุณได้รับเหรียญตราครบทุกเหรียญแล้ว ยอดเยี่ยมมาก!</Thai></p> : null}
    <Link to="/badges" className={buttonClass('secondary', 'w-full sm:w-auto')}><Label en="See all badges" th="ดูเหรียญตราทั้งหมด" /><IconArrowRight className="size-5 shrink-0" /></Link>
  </section>
}

/** The way to the certificate, where a finished reader looks first. */
export function CertificateReady() {
  return <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
    <p className="flex min-w-0 items-center gap-3 font-medium"><IconAward className="size-6 shrink-0" /><span>Your certificate is ready.<Thai tone="inherit" className="font-normal">ประกาศนียบัตรของคุณพร้อมแล้ว</Thai></span></p>
    <Link to="/certificate" className={buttonClass('primary')}><Label en="View certificate" th="ดูประกาศนียบัตร" /><IconArrowRight className="size-5 shrink-0" /></Link>
  </div>
}

type Refresh = () => void | Promise<boolean | void>

/** Every badge with its date or what earns it, the next one to aim for, and the certificate. */
export function BadgesPage({ quizzes, finalExam, certificate, loading = false, onQuiz, onRefresh, onBack }: {
  quizzes: QuizStanding[]; finalExam?: QuizStanding | null; certificate?: Certificate | null; loading?: boolean
  onQuiz: (chapter: number) => void; onRefresh: Refresh; onBack: () => void
}) {
  const list = badges(quizzes, finalExam, certificate)
  const earned = list.filter(badge => badge.earned).length
  // One clear next step: the first chapter badge still to earn whose quiz is open, then the Final Exam.
  const next = quizzes.filter(quiz => !quiz.passed && quiz.locked_by === null).sort((a, b) => a.chapter - b.chapter)[0]
  const finalNext = !next && !certificate && !!finalExam && !finalExam.passed && finalExam.locked_by === null
  return <main className="mx-auto w-full max-w-2xl space-y-5 px-4 py-6">
    <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
    <header className="space-y-2">
      <h1 className="text-2xl font-semibold">Your badges<Thai tone="inherit" size="text-lg" className="font-medium">เหรียญตราของคุณ</Thai></h1>
      <p className="text-neutral-600 dark:text-neutral-300">Earn a badge for each chapter quiz you pass. Pass them all for the All chapters badge, then pass the Final Exam to earn your certificate.
        <Thai className="mt-1">รับเหรียญตราทุกครั้งที่สอบผ่านแบบทดสอบท้ายบท สอบผ่านครบทุกบทเพื่อรับเหรียญครบทุกบท แล้วสอบผ่านแบบทดสอบปลายหลักสูตรเพื่อรับประกาศนียบัตร</Thai></p>
      {list.length && !loading ? <p className="font-medium tabular-nums">{earned} of {list.length} earned <span lang="th" className="font-normal">· ได้รับแล้ว {earned} จาก {list.length} เหรียญ</span></p> : null}
    </header>
    {/* Until the reading room arrives, nothing here may say a badge or the certificate is still to earn. */}
    {loading ? <p className="text-neutral-600 dark:text-neutral-300">Loading…</p> : <>
    {next ? <section aria-label="Next badge" className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border-2 border-neutral-900 bg-white p-4 dark:border-neutral-100 dark:bg-neutral-900">
      <p className="min-w-0 flex-1 basis-48"><span className="block text-sm text-neutral-600 dark:text-neutral-300">Next badge <span lang="th">· เหรียญถัดไป</span></span>
        <span className="block break-words font-semibold">Chapter {next.chapter} · {chapterTitle(next.category)}</span><Thai>{`บทที่ ${next.chapter}`}</Thai></p>
      {/* An unfinished chapter's quiz page lists what is left, with links. */}
      <Button onClick={() => onQuiz(next.chapter)} className="w-full sm:w-auto"><IconQuiz className="size-5 shrink-0" />{next.unfinished?.length
        ? <Label en="See what is left" th="ดูสิ่งที่ต้องเรียนต่อ" /> : <Label en="Take the quiz" th="ทำแบบทดสอบ" />}</Button>
    </section> : finalNext ? <section aria-label="Next badge" className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border-2 border-neutral-900 bg-white p-4 dark:border-neutral-100 dark:bg-neutral-900">
      <p className="min-w-0 flex-1 basis-48"><span className="block text-sm text-neutral-600 dark:text-neutral-300">Next badge <span lang="th">· เหรียญถัดไป</span></span>
        <span className="block break-words font-semibold">Final Exam <span lang="th" className="font-normal">· แบบทดสอบปลายหลักสูตร</span></span></p>
      <Button onClick={() => onQuiz(0)} className="w-full sm:w-auto"><IconQuiz className="size-5 shrink-0" /><Label en="Take the Final Exam" th="ทำแบบทดสอบปลายหลักสูตร" /></Button>
    </section> : null}
    {list.length ? <ol aria-label="Badges" className="grid grid-cols-2 gap-3 sm:grid-cols-3">{list.map(badge => {
      const aim = (badge.chapter !== undefined && badge.chapter === next?.chapter) || (badge.kind === 'final' && finalNext)
      return <li key={badge.key} className={`flex flex-col items-center gap-2 rounded-2xl border p-4 text-center ${badge.earned ? 'border-amber-300 bg-amber-50/70 dark:border-amber-700/70 dark:bg-amber-950/40' : aim ? 'border-2 border-neutral-900 bg-white dark:border-neutral-100 dark:bg-neutral-900' : 'border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900'}`}>
        {aim ? <p className="rounded-full bg-neutral-900 px-3 py-1 text-sm font-semibold text-white dark:bg-neutral-100 dark:text-neutral-900">Next <span lang="th" className="font-normal">· ถัดไป</span></p> : null}
        <Medal badge={badge} />
        <div className="min-w-0 space-y-1">
          <p className="font-semibold">{badge.title}<span lang="th" className="block text-sm font-normal text-neutral-600 dark:text-neutral-300">{badge.th}</span></p>
          <p className="break-words text-balance text-sm text-neutral-700 dark:text-neutral-200">{badge.subtitle}{badge.subtitleTh ? <Thai>{badge.subtitleTh}</Thai> : null}</p>
        </div>
        {badge.earned
          ? <p className="text-sm text-amber-900 dark:text-amber-200"><span className="block">Earned <span lang="th">· ได้รับเมื่อ</span></span><span className="block font-semibold">{badge.earnedAt ? on(badge.earnedAt) : '—'}</span></p>
          : <p className="text-balance text-sm text-neutral-600 dark:text-neutral-300">{badge.hint}<Thai>{badge.hintTh}</Thai></p>}
      </li>
    })}</ol> : <p className="text-neutral-600 dark:text-neutral-300">No chapter quizzes are published yet.<Thai className="mt-1">ยังไม่มีแบบทดสอบท้ายบท</Thai></p>}
    <section aria-labelledby="certificate-section" className="space-y-4 rounded-2xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
      <h2 id="certificate-section" className="text-lg font-semibold">Certificate of completion<Thai tone="inherit" size="text-base" className="font-medium">ประกาศนียบัตร</Thai></h2>
      {certificate ? <>
        <p className="text-neutral-700 dark:text-neutral-200">You passed the Final Exam on {on(certificate.awarded_at)}.<Thai className="mt-1">{`คุณสอบผ่านแบบทดสอบปลายหลักสูตรเมื่อ ${on(certificate.awarded_at)}`}</Thai></p>
        <CertificateCard certificate={certificate} level={3} />
        <Link to="/certificate" className={buttonClass('primary', 'w-full sm:w-auto')}><IconAward className="size-5 shrink-0" /><Label en="View your certificate" th="ดูประกาศนียบัตรของคุณ" /></Link>
      </> : finalExam?.passed ? <Preparing onRefresh={onRefresh} /> : finalExam ? <NotEarned finalExam={finalExam} onQuiz={onQuiz} /> : <Unavailable onRefresh={onRefresh} />}
    </section>
    </>}
  </main>
}

function NotEarned({ finalExam, onQuiz }: { finalExam?: QuizStanding | null; onQuiz: (chapter: number) => void }) {
  const open = !!finalExam && finalExam.locked_by === null
  return <div className="space-y-4">
    <p className="text-neutral-700 dark:text-neutral-200">Pass the Final Exam to earn your certificate. It shows your name, the date you passed and a certificate number.{finalExam?.locked_by != null ? ` The Final Exam opens after you pass the Chapter ${finalExam.locked_by} quiz.` : ''}
      <Thai className="mt-1">{`สอบผ่านแบบทดสอบปลายหลักสูตรเพื่อรับประกาศนียบัตร ซึ่งมีชื่อของคุณ วันที่สอบผ่าน และเลขที่ประกาศนียบัตร${finalExam?.locked_by != null ? ` แบบทดสอบปลายหลักสูตรจะเปิดหลังจากคุณสอบผ่านแบบทดสอบบทที่ ${finalExam.locked_by}` : ''}`}</Thai></p>
    {open ? <Button onClick={() => onQuiz(0)} className="w-full sm:w-auto"><IconQuiz className="size-5 shrink-0" /><Label en="Take the Final Exam" th="ทำแบบทดสอบปลายหลักสูตร" /></Button> : null}
  </div>
}

/** A manual check reports its outcome without clearing existing progress or dropping button focus. */
function CheckProgress({ onRefresh }: { onRefresh: Refresh }) {
  const [state, setState] = useState<'idle' | 'checking' | 'checked'>('idle')
  const [error, setError] = useState('')
  const busy = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  async function check() {
    if (busy.current) return
    busy.current = true
    setState('checking'); setError('')
    const identity = currentToken()
    try {
      const refreshed = onRefresh()
      const applied = refreshed === undefined ? undefined : await refreshed
      if (!mounted.current || currentToken() !== identity) return
      if (applied === false) throw new Error('Check superseded')
      setState('checked')
    } catch (failure) {
      if (!mounted.current || currentToken() !== identity) return
      setState('idle')
      const message = inline(failure) || 'Your progress could not be checked. Please try again.'
      setError(/[\u0E00-\u0E7F]/.test(message) ? message : `${message} / ตรวจสอบความคืบหน้าไม่สำเร็จ กรุณาลองใหม่อีกครั้ง`)
    } finally { busy.current = false }
  }
  return <div className="flex flex-col items-start gap-3">
    <Button variant="secondary" aria-busy={state === 'checking'} aria-disabled={state === 'checking'} onClick={() => void check()}><IconRetake className="size-5" />{state === 'checking'
      ? <Label en="Checking…" th="กำลังตรวจ…" /> : <Label en="Check again" th="ตรวจอีกครั้ง" />}</Button>
    <Message text={error} />
    <p role="status" className="text-sm text-neutral-700 empty:sr-only dark:text-neutral-200">{state === 'checked' ? <>Checked just now.<Thai>ตรวจเมื่อสักครู่</Thai></> : null}</p>
  </div>
}

/** No Final Exam standing to go by: the reading room did not load, a restore is being checked, or the exam is not
 *  published. A holder's certificate is hidden then too, so nothing here says it is still to earn. */
function Unavailable({ onRefresh }: { onRefresh: Refresh }) {
  return <div className="space-y-3">
    <p>Certificate details are not available right now. Please check again in a moment.<Thai className="mt-1">ยังแสดงข้อมูลประกาศนียบัตรไม่ได้ในขณะนี้ กรุณาตรวจอีกครั้งในอีกสักครู่</Thai></p>
    <CheckProgress onRefresh={onRefresh} />
  </div>
}

/** A pass the reading room has not brought a certificate for yet: recorded a moment ago, or answered by an
 *  API from before certificates while a deployment is in progress. */
function Preparing({ onRefresh }: { onRefresh: Refresh }) {
  return <div className="space-y-3">
    <p role="status">Your certificate is being prepared. It will appear here soon.<Thai className="mt-1">กำลังเตรียมประกาศนียบัตรของคุณ จะแสดงที่นี่เร็ว ๆ นี้</Thai></p>
    <CheckProgress onRefresh={onRefresh} />
  </div>
}

/** The full certificate page: the certificate, the name on it, and printing. */
export function CertificatePage({ certificate, finalExam, loading, onSaved, onRefresh, onPending, onQuiz, onBack }: {
  certificate?: Certificate | null; finalExam?: QuizStanding | null; loading: boolean
  onSaved: (certificate: Certificate) => void; onRefresh: Refresh; onPending?: () => void; onQuiz: (chapter: number) => void; onBack: () => void
}) {
  // A pass recorded a moment ago, or on another screen, before the reading room caught up: ask again.
  const pending = !certificate && !!finalExam?.passed
  const onRefreshRef = useRef(onPending ?? onRefresh)
  onRefreshRef.current = onPending ?? onRefresh
  useEffect(() => { if (pending) void Promise.resolve(onRefreshRef.current()).catch(() => { /* The manual check can retry. */ }) }, [pending])
  // Printing while the name is being changed would print the saved name, not the one on screen.
  const [editing, setEditing] = useState(false)
  // A certificate that goes away (a restore being checked) and comes back opens on its name, not an old edit.
  useEffect(() => { if (!certificate) setEditing(false) }, [certificate])
  return <main className="mx-auto w-full max-w-3xl space-y-5 px-4 py-6">
    <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
    <header className="space-y-1">
      <h1 className="text-2xl font-semibold">Your certificate<Thai tone="inherit" size="text-lg" className="font-medium">ประกาศนียบัตรของคุณ</Thai></h1>
      {certificate ? <p className="text-neutral-600 dark:text-neutral-300">Congratulations on passing the Final Exam.<Thai className="mt-1">ขอแสดงความยินดีที่คุณสอบผ่านแบบทดสอบปลายหลักสูตร</Thai></p> : null}
    </header>
    {certificate ? <>
      <CertificateCard certificate={certificate} />
      {/* On a phone the card is a picture whose small print cannot be read: the date and number again, full size. */}
      <p className="text-neutral-700 dark:text-neutral-200">Awarded {on(certificate.awarded_at, 'long')} · Certificate no. <span className="font-mono">{certificate.number}</span>
        <Thai className="mt-1">{`ให้ไว้ ณ วันที่ ${on(certificate.awarded_at, 'long')} · เลขที่ ${certificate.number}`}</Thai></p>
      <CertificateName certificate={certificate} onSaved={onSaved} editing={editing} onEditing={setEditing} />
      {editing ? <p className="text-sm text-amber-900 dark:text-amber-200">Save or cancel your name change before you print.<Thai tone="inherit" className="mt-1">บันทึกหรือยกเลิกการแก้ไขชื่อก่อนพิมพ์</Thai></p> : <div className="space-y-2">
        <Button onClick={() => window.print()} className="w-full sm:w-auto"><IconPrint className="size-5 shrink-0" /><Label en="Print or save as PDF" th="พิมพ์หรือบันทึกเป็น PDF" /></Button>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">To keep a PDF, choose Save as PDF in the print window. On an iPhone or iPad, tap the Share button in that window, then Save to Files. Landscape fits best.<Thai className="mt-1">หากต้องการเก็บเป็นไฟล์ PDF ให้เลือก “บันทึกเป็น PDF” ในหน้าต่างพิมพ์ ถ้าใช้ iPhone หรือ iPad ให้แตะปุ่มแชร์ในหน้าต่างนั้น แล้วเลือก “บันทึกไปยังไฟล์” ตั้งเป็นแนวนอนจะพอดีที่สุด</Thai></p>
      </div>}
      <PrintableCertificate certificate={certificate} />
    </> : loading ? <p className="text-neutral-600 dark:text-neutral-300">Loading…</p>
      : pending ? <section className="rounded-2xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900"><Preparing onRefresh={onRefresh} /></section>
        : <section className="rounded-2xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">{finalExam
          ? <NotEarned finalExam={finalExam} onQuiz={onQuiz} /> : <Unavailable onRefresh={onRefresh} />}</section>}
  </main>
}

/** The printed name: the one its owner saved, else the server's suggestion until they keep or change it. */
function CertificateName({ certificate, onSaved, editing, onEditing }: {
  certificate: Certificate; onSaved: (certificate: Certificate) => void; editing: boolean; onEditing: (editing: boolean) => void
}) {
  const [value, setValue] = useState(certificate.name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const nameHint = useId()
  const cleaned = tidyCertificateName(value)
  const length = [...cleaned].length
  const valid = length <= NAME_LIMIT && legibleName(cleaned)
  const field = useRef<HTMLInputElement>(null)
  const change = useRef<HTMLButtonElement>(null)
  const returning = useRef(false)
  useEffect(() => {
    if (editing) field.current?.focus()
    else if (returning.current) { returning.current = false; change.current?.focus() }
  }, [editing])
  const close = () => { returning.current = true; onEditing(false); setError('') }
  // While a name is saving, the form stays open (and Print hidden) until the answer, whichever button is pressed.
  const cancel = () => { if (!busy) close() }
  const open = () => { if (busy) return; setValue(certificate.name); setSaved(false); setError(''); onEditing(true) }

  /** Saves the name typed in the form, or with Keep this name the suggested one as it is. */
  async function save(name: string) {
    if (busy) return
    const cleanedName = tidyCertificateName(name)
    if (!legibleName(cleanedName) || [...cleanedName].length > NAME_LIMIT) {
      setError(`Enter your name in 1 to ${NAME_LIMIT} characters, with at least one letter or number. / กรุณาใส่ชื่อความยาว 1 ถึง ${NAME_LIMIT} ตัวอักษร และมีตัวอักษรหรือตัวเลขอย่างน้อยหนึ่งตัว`)
      field.current?.focus()
      return
    }
    setBusy(true); setError(''); setSaved(false)
    const identity = currentToken()
    try {
      const updated = await api.nameCertificate(cleanedName)
      // A late answer must not land on the next person's session.
      if (currentToken() !== identity) return
      onSaved(updated); setSaved(true)
      // Keep this name goes once the name is confirmed: focus moves to Change name, as after the form.
      if (editing) close(); else change.current?.focus()
    } catch (failure) {
      setError(inline(failure) || 'Your name could not be saved. Please try again. / บันทึกชื่อไม่สำเร็จ กรุณาลองใหม่อีกครั้ง')
      // Back to the name, to correct it or try again.
      field.current?.focus()
    } finally { setBusy(false) }
  }

  return <section aria-label="Name on your certificate" className="flex flex-col gap-3 rounded-2xl border border-neutral-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
    {editing ? <form onSubmit={event => { event.preventDefault(); void save(value) }} className="space-y-3">
      <label className="block space-y-2">
        <span className="block text-sm font-medium text-neutral-700 dark:text-neutral-300">Name on your certificate<span lang="th" className="block font-normal">ชื่อบนประกาศนียบัตร</span></span>
        {/* No autofill: on a shared tablet it would offer the device owner's name. */}
        <input ref={field} className={input} value={value} aria-describedby={nameHint} aria-invalid={!!value && !valid} autoComplete="off" onChange={event => { setValue(event.target.value); setError('') }} />
      </label>
      <p id={nameHint} className={`text-sm ${valid ? 'text-neutral-600 dark:text-neutral-300' : 'text-amber-900 dark:text-amber-200'}`}><span>{length} / {NAME_LIMIT} characters</span><Thai tone="inherit">{`${length} / ${NAME_LIMIT} ตัวอักษร`}</Thai></p>
      <Message text={error} />
      <div className="flex flex-col gap-3 sm:flex-row">
        {/* Not disabled while saving, which would drop keyboard focus; save() ignores a second press. */}
        <Button type="submit" disabled={!valid} aria-disabled={busy}>{busy ? <Label en="Saving…" th="กำลังบันทึก…" /> : <Label en="Save name" th="บันทึกชื่อ" />}</Button>
        <Button type="button" variant="secondary" aria-disabled={busy} onClick={cancel}><Label en="Cancel" th="ยกเลิก" /></Button>
      </div>
    </form> : <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="min-w-0"><span className="block text-sm text-neutral-600 dark:text-neutral-300">Name on your certificate <span lang="th">· ชื่อบนประกาศนียบัตร</span></span><span className="block break-words text-lg font-semibold">{certificate.name}</span></p>
      <div className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row">
        {!certificate.name_confirmed ? <Button variant="secondary" aria-disabled={busy} onClick={() => void save(certificate.name)}><IconCheck className="size-5 shrink-0" />{busy
          ? <Label en="Saving…" th="กำลังบันทึก…" /> : <Label en="Keep this name" th="ใช้ชื่อนี้" />}</Button> : null}
        <Button ref={change} variant="secondary" aria-disabled={busy} onClick={open}><IconRename className="size-5 shrink-0" /><Label en="Change name" th="แก้ไขชื่อ" /></Button>
      </div>
    </div>}
    {editing ? null : <Message text={error} />}
    {!certificate.name_confirmed && !editing ? <p className="text-sm text-amber-900 dark:text-amber-200">Check your name before you print. Keep it if it is right, or change it, for example to your full name.<Thai tone="inherit" className="mt-1">ตรวจสอบชื่อก่อนพิมพ์ ถ้าถูกต้องให้กด “ใช้ชื่อนี้” หรือแก้ไข เช่น ใส่ชื่อและนามสกุลเต็ม</Thai></p> : null}
    <p role="status" className="text-sm text-neutral-700 empty:sr-only dark:text-neutral-200">{saved ? <>Saved. Your certificate shows this name.<Thai className="mt-0.5">บันทึกแล้ว ประกาศนียบัตรจะแสดงชื่อนี้</Thai></> : null}</p>
  </section>
}

/** The certificate itself, laid out in container units so the screen copy scales like a picture and the
 *  printed copy fills a landscape page. Always paper-coloured, whatever the theme. */
export function CertificateCard({ certificate, print = false, level = 2 }: { certificate: Certificate; print?: boolean; level?: 2 | 3 }) {
  const { organization_name: organization } = useBranding()
  // Its title sits under the page's heading: h3 inside a section that has its own h2.
  const Title = level === 3 ? 'h3' : 'h2'
  const length = certificate.name.length
  const size = length > 40 ? 'text-[3.4cqw]' : length > 26 ? 'text-[4.4cqw]' : 'text-[5.8cqw]'
  return <article aria-label="Certificate of completion" className={`@container relative w-full overflow-hidden bg-[radial-gradient(ellipse_at_center,#fffdf8_0%,#fbf5e6_100%)] text-neutral-900 ${print ? 'h-full' : 'aspect-[11/8.5] rounded-lg shadow-lg ring-1 ring-black/10'}`}>
    <div aria-hidden="true" className="absolute inset-[2.2cqw] border-[0.6cqw] border-double border-emerald-800" />
    <div aria-hidden="true" className="absolute inset-[3.7cqw] border-[0.16cqw] border-amber-500" />
    {['left-[3cqw] top-[3cqw]', 'right-[3cqw] top-[3cqw]', 'bottom-[3cqw] left-[3cqw]', 'bottom-[3cqw] right-[3cqw]'].map(place =>
      <svg key={place} aria-hidden="true" viewBox="0 0 10 10" className={`absolute ${place} w-[1.4cqw] fill-amber-500`}><path d="M5 0l5 5-5 5-5-5z" /></svg>)}
    <div className="relative flex h-full flex-col items-center justify-between px-[8cqw] pb-[5.4cqw] pt-[6cqw] text-center">
      <div className="flex flex-col items-center">
        <p className="max-w-full break-words text-[1.4cqw] font-semibold uppercase tracking-[0.42em] text-emerald-800">{organization || 'Staff training'}</p>
        <Title className="mt-[1.4cqw] font-serif text-[5.6cqw] leading-none tracking-tight">Certificate of Completion</Title>
        <p lang="th" className="mt-[1.2cqw] text-[2.5cqw] font-semibold leading-tight text-emerald-900">ประกาศนียบัตร</p>
      </div>
      <div className="flex w-full flex-col items-center">
        <p className="font-serif text-[1.8cqw] italic text-neutral-700">This certifies that</p>
        <p lang="th" className="text-[1.5cqw] text-neutral-600">ขอมอบประกาศนียบัตรฉบับนี้เพื่อแสดงว่า</p>
        <p className={`mt-[1.4cqw] w-[72%] break-words border-b-[0.18cqw] border-amber-500 pb-[1cqw] font-serif leading-tight text-emerald-950 ${size}`}>{certificate.name}</p>
        <p className="mt-[2cqw] max-w-[80%] text-[1.7cqw] leading-snug text-neutral-800">has completed the {organization ? `${organization} ` : ''}staff training and passed the Final Exam.</p>
        <p lang="th" className="mt-[0.5cqw] max-w-[80%] text-[1.6cqw] leading-snug text-neutral-700">ได้ผ่านการฝึกอบรมพนักงาน{organization ? `ของ ${organization}` : ''} และสอบผ่านแบบทดสอบปลายหลักสูตร</p>
      </div>
      <div className="grid w-full grid-cols-[1fr_auto_1fr] items-end gap-[3cqw]">
        <div className="text-left">
          <p className="text-[1.15cqw] font-semibold uppercase tracking-[0.2em] text-neutral-500">Awarded <span lang="th" className="normal-case tracking-normal">· ให้ไว้ ณ วันที่</span></p>
          <p className="mt-[0.5cqw] border-t-[0.12cqw] border-neutral-400 pt-[0.6cqw] text-[1.6cqw] font-medium">{on(certificate.awarded_at, 'long')}</p>
        </div>
        <Seal className="w-[12.5cqw]" />
        <div className="text-right">
          <p className="text-[1.15cqw] font-semibold uppercase tracking-[0.2em] text-neutral-500">Certificate no. <span lang="th" className="normal-case tracking-normal">· เลขที่</span></p>
          <p className="mt-[0.5cqw] border-t-[0.12cqw] border-neutral-400 pt-[0.6cqw] font-mono text-[1.6cqw] font-medium">{certificate.number}</p>
          <p className="text-[1.3cqw] text-neutral-600">Issued through Postern <span lang="th">· ออกผ่านระบบ Postern</span></p>
        </div>
      </div>
    </div>
  </article>
}

/** A gold seal around the n of the Postern wordmark, its doorway open. */
function Seal({ className = '' }: { className?: string }) {
  const { organization_name: organization } = useBranding()
  const id = `seal${useId().replace(/[^\w-]/g, '')}`
  return <svg aria-hidden="true" viewBox="0 0 120 120" className={className}>
    <defs><Gold id={`${id}-gold`} /><path id={`${id}-ring`} d="M60 27a33 33 0 1 1 0 66a33 33 0 1 1 0-66" /></defs>
    <path d={SEAL_EDGE} fill={`url(#${id}-gold)`} />
    <circle cx="60" cy="60" r="47" fill="#065f46" stroke="#fcd34d" strokeWidth="1.5" />
    <circle cx="60" cy="60" r="41.5" fill="none" stroke="#fcd34d" strokeWidth=".8" strokeDasharray="1.5 2" />
    <text fill="#fde68a" fontSize="7.4" fontWeight="700" fontFamily="ui-sans-serif, system-ui, sans-serif">
      <textPath href={`#${id}-ring`} textLength="203" lengthAdjust="spacing">{organization ? `${organization.toUpperCase()} • ` : ''}FINAL EXAM PASSED •</textPath>
    </text>
    <path d="M45 80V60a15 15 0 0 1 30 0v20z" fill="#fbbf24" />
    <path d="M53.5 80V59.5a6.5 6.5 0 0 1 13 0V80z" fill="#065f46" />
  </svg>
}

/** Browsers name a saved PDF after the page title. It carries no name or number: a shared tablet keeps its history. */
/** What the browser prints: this certificate alone (index.css hides everything else), on one landscape
 *  page. The page rule lives here, so printing any other screen keeps the browser's own settings. With no
 *  page margin Chrome leaves out its headers and footers (date, title and address); index.css insets the copy.
 *  While mounted, the page uses the installation's nonpersonal certificate title for saved PDFs. */
export function PrintableCertificate({ certificate }: { certificate: Certificate }) {
  const { certificate_title: title } = useBranding()
  useEffect(() => {
    const before = document.title
    document.title = title
    return () => { document.title = before }
  }, [title])
  return createPortal(<div className="print-root">
    <style>{'@page { size: landscape; margin: 0; }'}</style>
    <CertificateCard certificate={certificate} print />
  </div>, document.body)
}
