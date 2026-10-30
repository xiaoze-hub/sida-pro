import * as React from 'react'
import { cn } from '../../cn'

interface CollapsibleContextValue {
  open: boolean
  setOpen: (open: boolean | ((prev: boolean) => boolean)) => void
  disabled?: boolean
}

const CollapsibleContext = React.createContext<CollapsibleContextValue | null>(null)

export interface CollapsibleProps extends React.HTMLAttributes<HTMLDivElement> {
  open?: boolean
  defaultOpen?: boolean
  onOpenChange?: (open: boolean) => void
  disabled?: boolean
}

export const Collapsible = React.forwardRef<HTMLDivElement, CollapsibleProps>(
  ({ open: controlledOpen, defaultOpen = false, onOpenChange, disabled, className, children, ...props }, ref) => {
    const [uncontrolledOpen, setUncontrolledOpen] = React.useState(defaultOpen)
    const isControlled = controlledOpen !== undefined
    const open = isControlled ? controlledOpen : uncontrolledOpen

    const setOpen = React.useCallback(
      (value: boolean | ((prev: boolean) => boolean)) => {
        const next = typeof value === 'function' ? value(open) : value
        if (!isControlled) {
          setUncontrolledOpen(next)
        }
        onOpenChange?.(next)
      },
      [isControlled, open, onOpenChange]
    )

    return (
      <CollapsibleContext.Provider value={{ open, setOpen, disabled }}>
        <div ref={ref} data-state={open ? 'open' : 'closed'} className={cn(className)} {...props}>
          {children}
        </div>
      </CollapsibleContext.Provider>
    )
  }
)
Collapsible.displayName = 'Collapsible'

export interface CollapsibleTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  asChild?: boolean
}

export const CollapsibleTrigger = React.forwardRef<HTMLButtonElement, CollapsibleTriggerProps>(
  ({ asChild, onClick, className, children, ...props }, ref) => {
    const ctx = React.useContext(CollapsibleContext)
    if (!ctx) throw new Error('CollapsibleTrigger must be used within Collapsible')

    const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
      onClick?.(e)
      if (!ctx.disabled) {
        ctx.setOpen((prev) => !prev)
      }
    }

    if (asChild && React.isValidElement(children)) {
      return React.cloneElement(children as React.ReactElement<any>, {
        ref,
        onClick: (e: any) => {
          (children as any).props?.onClick?.(e)
          handleClick(e)
        },
        'data-state': ctx.open ? 'open' : 'closed',
        'aria-expanded': ctx.open,
      })
    }

    return (
      <button
        ref={ref}
        type="button"
        aria-expanded={ctx.open}
        data-state={ctx.open ? 'open' : 'closed'}
        disabled={ctx.disabled}
        onClick={handleClick}
        className={cn(className)}
        {...props}
      >
        {children}
      </button>
    )
  }
)
CollapsibleTrigger.displayName = 'CollapsibleTrigger'

export interface CollapsibleContentProps extends React.HTMLAttributes<HTMLDivElement> {
  forceMount?: boolean
}

export const CollapsibleContent = React.forwardRef<HTMLDivElement, CollapsibleContentProps>(
  ({ className, children, forceMount = false, ...props }, ref) => {
    const ctx = React.useContext(CollapsibleContext)
    if (!ctx) throw new Error('CollapsibleContent must be used within Collapsible')

    if (!ctx.open && !forceMount) {
      return null
    }

    return (
      <div
        ref={ref}
        data-state={ctx.open ? 'open' : 'closed'}
        className={cn(
          'grid transition-[grid-template-rows] duration-200 ease-out animate-in fade-in-0',
          ctx.open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
          className
        )}
        {...props}
      >
        <div className="overflow-hidden">{children}</div>
      </div>
    )
  }
)
CollapsibleContent.displayName = 'CollapsibleContent'
