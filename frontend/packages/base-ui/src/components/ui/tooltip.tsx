import * as React from 'react'
import * as PopoverPrimitive from '@radix-ui/react-popover'
import { cn } from '../../cn'

interface TooltipContextValue {
  open: boolean
  setOpen: (open: boolean) => void
}

const TooltipContext = React.createContext<TooltipContextValue | null>(null)

export function TooltipProvider({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

export interface TooltipProps {
  children: React.ReactNode
  open?: boolean
  defaultOpen?: boolean
  onOpenChange?: (open: boolean) => void
  delayDuration?: number
}

export function Tooltip({
  children,
  open: controlledOpen,
  defaultOpen = false,
  onOpenChange,
  delayDuration = 120,
}: TooltipProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(defaultOpen)
  const isControlled = controlledOpen !== undefined
  const open = isControlled ? controlledOpen : uncontrolledOpen
  const timerRef = React.useRef<number | null>(null)

  const clearTimer = React.useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const setOpen = React.useCallback(
    (next: boolean) => {
      clearTimer()
      if (!isControlled) {
        setUncontrolledOpen(next)
      }
      onOpenChange?.(next)
    },
    [clearTimer, isControlled, onOpenChange]
  )

  const scheduleOpen = React.useCallback(() => {
    clearTimer()
    timerRef.current = window.setTimeout(() => {
      setOpen(true)
    }, delayDuration)
  }, [clearTimer, delayDuration, setOpen])

  const scheduleClose = React.useCallback(() => {
    clearTimer()
    timerRef.current = window.setTimeout(() => {
      setOpen(false)
    }, 60)
  }, [clearTimer, setOpen])

  React.useEffect(() => {
    return () => clearTimer()
  }, [clearTimer])

  return (
    <TooltipContext.Provider value={{ open, setOpen }}>
      <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
        <div
          className="inline-flex"
          onMouseEnter={scheduleOpen}
          onMouseLeave={scheduleClose}
          onFocus={scheduleOpen}
          onBlur={scheduleClose}
        >
          {children}
        </div>
      </PopoverPrimitive.Root>
    </TooltipContext.Provider>
  )
}

export const TooltipTrigger = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { asChild?: boolean }
>(({ asChild, className, children, ...props }, ref) => {
  return (
    <PopoverPrimitive.Trigger asChild={asChild} ref={ref} className={className} {...props}>
      {children}
    </PopoverPrimitive.Trigger>
  )
})
TooltipTrigger.displayName = 'TooltipTrigger'

export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content>
>(({ className, side = 'top', align = 'center', sideOffset = 4, children, ...props }, ref) => {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        ref={ref}
        side={side}
        align={align}
        sideOffset={sideOffset}
        className={cn(
          'z-50 pointer-events-none select-none rounded-md border border-border bg-popover px-2.5 py-1 text-[12px] font-medium text-popover-foreground shadow-float transition-opacity data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
          className
        )}
        {...props}
      >
        {children}
      </PopoverPrimitive.Content>
    </PopoverPrimitive.Portal>
  )
})
TooltipContent.displayName = 'TooltipContent'
