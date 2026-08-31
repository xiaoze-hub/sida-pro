import type { MouseEventHandler } from 'react'
import { AlarmClock } from 'lucide-react'
import { cn } from '@panwatch/base-ui'
import { BadgeChip, type BadgeChipSize } from '@panwatch/biz-ui/components/badge-chip'
import { normalizeSuggestionAction, resolveSuggestionColorClass, resolveSuggestionLabel } from '@panwatch/biz-ui/components/suggestion-action'

interface AiSuggestionBadgeProps {
  action?: string
  actionLabel?: string
  isAI?: boolean
  isExpired?: boolean
  size?: BadgeChipSize
  className?: string
  title?: string
  onClick?: MouseEventHandler<HTMLButtonElement>
  iconOnlyWhenAlert?: boolean
}

export function AiSuggestionBadge({
  action,
  actionLabel,
  isAI = false,
  isExpired = false,
  size = 'md',
  className,
  title,
  onClick,
  iconOnlyWhenAlert = false,
}: AiSuggestionBadgeProps) {
  const label = resolveSuggestionLabel(action, actionLabel)
  const colorClass = resolveSuggestionColorClass(action, actionLabel)
  if (iconOnlyWhenAlert && normalizeSuggestionAction(action, actionLabel) === 'alert') {
    const dimension = size === 'lg' ? 'h-8 w-8' : size === 'xs' ? 'h-5 w-5' : 'h-6 w-6'
    const iconSize = size === 'lg' ? 'h-4 w-4' : size === 'xs' ? 'h-3 w-3' : 'h-3.5 w-3.5'
    return (
      <button
        type="button"
        aria-label={label}
        title={title || label}
        onClick={onClick}
        className={cn(
          'relative inline-flex shrink-0 items-center justify-center rounded-full bg-blue-500 text-white shadow-sm transition-all hover:bg-blue-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50',
          dimension,
          isExpired && 'opacity-50',
          className,
        )}
      >
        <AlarmClock className={iconSize} />
        {isAI && (
          <span className="absolute -right-1 -top-1 rounded-full bg-indigo-600 px-1 text-[7px] font-bold leading-[12px] text-white ring-1 ring-card">
            AI
          </span>
        )}
      </button>
    )
  }
  return (
    <BadgeChip
      label={label}
      aiTag={isAI}
      size={size}
      title={title}
      onClick={onClick}
      className={cn(colorClass, isExpired && 'opacity-50', className)}
    />
  )
}
