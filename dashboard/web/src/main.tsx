import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
import { AccountsApp } from './accounts'
import { accountsEnabled } from './accounts-api'
import './index.css'

createRoot(document.getElementById('root')!).render(<StrictMode>{accountsEnabled() ? <AccountsApp /> : <App />}</StrictMode>)
