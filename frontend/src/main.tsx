import { createRoot } from 'react-dom/client'
import { TooltipProvider } from '@/components/ui/tooltip'
import App from './dashboard'
import './index.css'

window.addEventListener('hashchange', () => {
  if (new URLSearchParams(location.hash.slice(1)).get('session')) location.reload()
})

createRoot(document.getElementById('root')!).render(<TooltipProvider><App /></TooltipProvider>)
