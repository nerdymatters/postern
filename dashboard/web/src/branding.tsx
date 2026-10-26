import { createContext, useContext } from 'react'

export interface Branding {
  organization_name: string
  privacy_contact_email: string
  certificate_title: string
}

function text(value: unknown, max: number): string {
  return typeof value === 'string' && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value) ? value.trim() : ''
}

/** Build-time identity keeps the public privacy page useful while the API is down. */
export function publicBranding(value?: Partial<Branding>): Branding {
  const organization = text(import.meta.env.VITE_POSTERN_ORGANIZATION_NAME, 80)
  const contact = text(import.meta.env.VITE_POSTERN_PRIVACY_CONTACT_EMAIL, 254)
  const email = text(value?.privacy_contact_email ?? contact, 254)
  return {
    organization_name: text(value?.organization_name ?? organization, 80),
    privacy_contact_email: /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email) ? email : '',
    certificate_title: text(value?.certificate_title, 80) || 'Training Certificate',
  }
}

const BrandingContext = createContext<Branding>(publicBranding())
export const BrandingProvider = BrandingContext.Provider
export function useBranding() { return useContext(BrandingContext) }
