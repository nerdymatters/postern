import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api, currentToken, inline, onRefusal, setToken, type Certificate, type Config, type Identity, type Post, type QuizResult, type QuizStanding, type Refusal, type Session, type SignIn, type Wall } from './api'
import { releaseAll } from './blobs'
import { SelfieEnrollment } from './selfie'
import { TrainingDashboard } from './training-progress'
import { Admin } from './admin'
import { Consent } from './consent'
import { Grid } from './grid'
import { BadgesPage, CertificatePage } from './honors'
import { Landing } from './landing'
import { Privacy } from './privacy'
import { lockedBy, QuizPage } from './quiz'
import { Reader } from './reader'
import { Refused } from './refusal'
import { Denials, Report } from './report'
import { Link, navigate, useRoute } from './routes'
import { Button, Centred, Message, Panel, Wordmark, Label } from './ui'
import { forgetViews } from './views'
import { ThemeToggle, useTheme } from './theme'
import { BrandingProvider, publicBranding } from './branding'
import { sameIdentity } from './accounts-api'
import { IconAdmin, IconSignOut, IconUpdateSelfie } from './PosternIcons'

// Phones get icon-only header items in 44 px tap targets; the words stay for screen readers.
const HEADER_ITEM = 'header-action'
const HEADER_ICON = 'size-5 sm:size-4'
const HEADER_LABEL = 'sr-only sm:not-sr-only'

export function App({ expectedIdentity, onSessionLost, onAccountSignOut }: { expectedIdentity?: Identity; onSessionLost?: () => void; onAccountSignOut?: () => void } = {}) {
  const { dark, toggle } = useTheme()
  const route = useRoute()
  const adminRoute = ['admin', 'progress', 'report', 'denials'].includes(route.name)
  const [config, setConfig] = useState<Config | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [posts, setPosts] = useState<Post[]>([])
  const [quizzes, setQuizzes] = useState<QuizStanding[]>([])
  const [exempt, setExempt] = useState(false)
  const [finalExam, setFinalExam] = useState<QuizStanding | null>(null)
  // Undefined until a wall says otherwise: an API from before certificates sends none, which is not "not earned".
  const [certificate, setCertificate] = useState<Certificate | null | undefined>(undefined)
  const show = useCallback((wall: Wall) => { setPosts(wall.posts); setQuizzes(wall.quizzes ?? []); setExempt(wall.quiz_exempt ?? false); setFinalExam(wall.final_exam ?? null); setCertificate(wall.certificate) }, [])
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  const [notice, setNotice] = useState('')
  const [trouble, setTrouble] = useState('')
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)
  const reload = useCallback(() => setAttempt(count => count + 1), [])
  const [renewing, setRenewing] = useState(false)
  const selfieSaved = useCallback(() => { setRenewing(false); forgetViews(); releaseAll(); reload() }, [reload])
  const [progressVersion, setProgressVersion] = useState(0)
  const refreshProgress = useCallback(() => setProgressVersion(value => value + 1), [])

  useEffect(() => {
    if (!progressVersion || !session || session.consent_required || session.selfie?.required) return
    let live = true
    const identity = currentToken()
    const timer = window.setTimeout(() => {
      api.posts().then(result => {
        if (live && currentToken() === identity) show(result)
      }).catch(() => { /* Existing progress stays visible; refusals use the shared handler. */ })
    }, 250)
    return () => { live = false; window.clearTimeout(timer) }
  }, [progressVersion, session?.email, session?.consent_required, session?.selfie?.required, route.name])

  // Refusals clear protected media before showing the required next step.
  useEffect(() => {
    return onRefusal(refused => {
      if (expectedIdentity && refused.code === 'sign_in_required') {
        setToken(null); forgetViews(); releaseAll(); onSessionLost?.(); return
      }
      if (refused.code === 'sign_in_required') {
        setToken(null)
        forgetViews()
        releaseAll()
        // The next person on a shared tablet must not see this reader's progress or quiz results.
        show({ posts: [] })
        setSession(null)
        setRefusal(null)
        setNotice('Please sign in again.')
      } else if (refused.code === 'selfie_required') {
        forgetViews(); releaseAll()
        setSession(current => current && { ...current, selfie: { enabled: true, required: true } })
      } else if (refused.code === 'consent_required') {
        setSession(current => current && { ...current, consent_required: true })
      } else {
        setRefusal(refused)
      }
    })
  }, [show, expectedIdentity, onSessionLost])

  useEffect(() => {
    let live = true
    setLoading(true)
    void (async () => {
      try {
        const settings = await (expectedIdentity ? api.configForAccount() : api.config())
        if (!live) return
        setConfig(settings)
        setTrouble('')
        const identity = currentToken()
        if (!identity) {
          setSession(null)
          if (expectedIdentity) onSessionLost?.()
          return
        }
        // A reader who signs out while this loads must not leave their training behind for the next person.
        const current = await (expectedIdentity ? api.sessionForAccount({ quietRefusal: false }) : api.session())
        if (!live || currentToken() !== identity) return
        if (expectedIdentity && !sameIdentity(expectedIdentity, current.identity)) {
          setToken(null); forgetViews(); releaseAll(); onSessionLost?.(); return
        }
        setSession(current)
        if (current.consent_required || current.selfie?.required) return
        const wall = await api.posts()
        if (live && currentToken() === identity) show(wall)
      } catch (failure) {
        if (live) setTrouble(inline(failure))
      } finally {
        if (live) setLoading(false)
      }
    })()
    return () => { live = false }
  }, [attempt, expectedIdentity, onSessionLost])

  async function leave() {
    if (expectedIdentity) {
      // The account owner captures and revokes both credentials before clearing them.
      onAccountSignOut?.()
      return
    }
    try {
      await api.signOut()
    } catch {
      // The local session is dropped whether or not the server heard about it.
    }
    setToken(null)
    forgetViews()
    releaseAll()
    setSession(null)
    setRefusal(null)
    show({ posts: [] })
    setRenewing(false)
    setNotice('You are signed out.')
    navigate('/')
  }

  // Unlock the next chapter at once; the refreshed wall then confirms it.
  function finished(result: QuizResult) {
    setQuizzes(result.quizzes)
    if (result.final_exam !== undefined) setFinalExam(result.final_exam)
    setPosts(current => current.map(post => ({ ...post, locked_by: lockedBy(post.chapter, result.quizzes, exempt) })))
    refreshProgress()
  }

  function entered(result: SignIn) {
    if (expectedIdentity && !sameIdentity(expectedIdentity, result.identity)) {
      setToken(null); forgetViews(); releaseAll(); onSessionLost?.(); return
    }
    setToken(result.token)
    setNotice('')
    // Nothing of a previous reader's training, certificate included, stays on screen for the next one.
    show({ posts: [] })
    reload()
  }

  const branding = publicBranding(config?.branding)
  if (route.name === 'privacy') return <BrandingProvider value={branding}><Privacy /></BrandingProvider>
  if (!config) {
    return loading ? <Centred title="Postern"><p className="text-neutral-600 dark:text-neutral-300">Loading…</p></Centred> : (
      <Centred title="Postern is not answering">
        <Panel className="space-y-3">
          <Message text={trouble} />
          <Button onClick={reload} className="w-full">Try again</Button>
        </Panel>
      </Centred>
    )
  }
  if (refusal) return <Refused refusal={refusal} onRetry={() => { setRefusal(null); reload() }} onSignOut={leave} />
  if (!session && expectedIdentity) return loading ? <Centred title="Checking private access"><p role="status">Checking your space session…</p></Centred> : (
    <Centred title="Private space is unavailable">
      <Panel className="space-y-4">
        <Message text={trouble || 'Your private access could not be checked. Your Postern account is still available.'} />
        <Button onClick={reload}>Try again</Button>
        <Link to="/home" className="block underline">Back to My Posterns</Link>
      </Panel>
    </Centred>
  )
  if (!session) return <BrandingProvider value={branding}><Landing config={config} notice={notice} onSignedIn={entered} /></BrandingProvider>
  if (session.consent_required) return <Consent config={config} onAccepted={reload} onDeclined={leave} />
  if ((session.selfie?.required && !(session.role === 'admin' && adminRoute)) || renewing) return <SelfieEnrollment config={config} initial={session.selfie} onSaved={selfieSaved} onLeave={leave} onCancel={renewing && !session.selfie?.required ? () => setRenewing(false) : undefined} onAdmin={session.role === 'admin' && session.selfie?.required ? () => { setRenewing(false); navigate('/admin') } : undefined} />

  const post = route.name === 'post' ? posts.find(candidate => candidate.id === route.id) : undefined
  const takeQuiz = (chapter: number) => navigate(chapter === 0 ? "/final-exam" : `/quiz/${chapter}`)
  return (
    <BrandingProvider value={branding}><div className="min-h-full pb-10">
      {config.preview ? <p className="bg-amber-50 dark:bg-amber-950 p-2 text-center text-xs text-amber-900 dark:text-amber-200">Selfie preview · Separate from staff training</p> : null}
      <header className="sticky top-0 z-10 border-b border-neutral-200 dark:border-neutral-800 bg-white/95 dark:bg-neutral-900/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-2xl items-center justify-between px-4 py-1 sm:py-2">
          <Link to="/"><Wordmark /></Link>
          <nav className="flex items-center gap-1 text-sm sm:gap-2">
            <ThemeToggle dark={dark} onToggle={toggle} />
            {session.role === 'admin' ? <Link to="/admin" className={HEADER_ITEM}><IconAdmin className={HEADER_ICON} /><span className={HEADER_LABEL}>Admin</span></Link> : null}
            {config.selfie_enabled ? <button onClick={() => setRenewing(true)} className={HEADER_ITEM}><IconUpdateSelfie className={HEADER_ICON} /><span className={HEADER_LABEL}>Update selfie</span></button> : null}
            <button onClick={leave} className={HEADER_ITEM}><IconSignOut className={HEADER_ICON} /><span className={HEADER_LABEL}>Sign out</span></button>
          </nav>
        </div>
      </header>

      {session.selfie?.required && session.role === 'admin' ? <p className="mx-auto max-w-2xl px-4 pt-4 text-neutral-600 dark:text-neutral-300">You can manage content without a selfie. To open training materials or view enrollment photos, complete your selfie first.</p> : null}

      {session.selfie?.expires_at && session.selfie.expires_at < Date.now()/1000 + 5*86400 ? <p className="mx-auto max-w-2xl px-4 pt-4 text-sm text-amber-800 dark:text-amber-200">Your selfie expires soon. <button className="underline" onClick={() => setRenewing(true)}>Update it now</button> to keep reading without interruption.</p> : null}
      {route.name === 'grid' ? <Grid posts={posts} quizzes={quizzes} finalExam={finalExam} certificate={certificate} exempt={exempt} onQuiz={takeQuiz} onOpen={(chosen, item) => navigate(`/post/${chosen.id}${item ? `/item/${item.id}` : ''}`)} /> : null}
      {route.name === 'badges' ? <BadgesPage quizzes={quizzes} finalExam={finalExam} certificate={certificate} loading={loading} onQuiz={takeQuiz} onRefresh={refreshProgress} onBack={() => navigate('/')} /> : null}
      {route.name === 'certificate' ? <CertificatePage certificate={certificate} finalExam={finalExam} loading={loading} onSaved={saved => { setCertificate(saved); refreshProgress() }} onRefresh={refreshProgress} onQuiz={takeQuiz} onBack={() => navigate('/')} /> : null}
      {route.name === 'quiz' ? <QuizPage key={route.chapter} chapter={route.chapter} posts={posts} exempt={route.chapter === 0 ? finalExam?.optional ?? false : exempt} onQuiz={takeQuiz} onFinished={finished} onStale={refreshProgress} onOpen={(chosen, item) => navigate(`/post/${chosen.id}${item ? `/item/${item.id}` : ''}`)} onBack={() => navigate('/')} /> : null}
      {route.name === 'post' ? (
        post
          ? <Reader key={`${post.id}:${route.item ?? ''}`} selectedItem={route.item} finalExam={finalExam} post={post} posts={posts} quizzes={quizzes} exempt={exempt} onQuiz={takeQuiz} onBack={() => navigate('/')} onOpen={(chosen, item) => navigate(`/post/${chosen.id}${item ? `/item/${item.id}` : ''}`)} onPageDisplayed={refreshProgress} />
          : (
            <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-10">
              <Message text={loading ? 'Loading…' : 'That post is not available to you.'} />
              <Button variant="secondary" onClick={() => navigate('/')}><Label en="Back to your training" th="กลับไปหน้าการเรียน" /></Button>
            </main>
          )
      ) : null}
      {route.name === 'admin' ? <AdminOnly session={session}><Admin selfieEnabled={config.selfie_enabled} onMetadataChanged={reload} /></AdminOnly> : null}
      {route.name === 'progress' ? <AdminOnly session={session}><TrainingDashboard /></AdminOnly> : null}
      {route.name === 'report' ? <AdminOnly session={session}><Report postId={route.id} onBack={() => navigate('/admin')} /></AdminOnly> : null}
      {route.name === 'denials' ? <AdminOnly session={session}><Denials onBack={() => navigate('/admin')} /></AdminOnly> : null}

      <footer className="mx-auto w-full max-w-2xl px-4 pt-6 text-center text-xs text-neutral-500 dark:text-neutral-400">
        Signed in as {session.email}
      </footer>
    </div></BrandingProvider>
  )
}

function AdminOnly({ session, children }: { session: Session; children: ReactNode }) {
  if (session.role === 'admin') return children
  return (
    <main className="mx-auto w-full max-w-2xl px-4 py-10">
      <Message text="Administrator access is required." />
    </main>
  )
}
