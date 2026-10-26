
import { useSettings } from './context'

export function SettingsFooter() {
  const {
    version,
  } = useSettings()
  return (
    <>
{/* Version Footer */}
{version && (
  <div className="mt-8 text-center text-[12px] text-muted-foreground">
    数智分析 v{version}
  </div>
)}
    </>
  )
}
