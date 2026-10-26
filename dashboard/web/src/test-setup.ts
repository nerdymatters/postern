import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'
import { installStubs, resetStubs } from './testing'

installStubs()
beforeEach(resetStubs)
afterEach(cleanup)
