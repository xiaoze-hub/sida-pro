import * as React from 'react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { cn } from '../../cn'

export const TooltipProvider = TooltipPrimitive.Provider
export type TooltipProps = React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Root>

export function Tooltip({ children, ...props }: TooltipProps) {
  return (
    <TooltipPrimitive.Provider delayDuration={120}>
      <TooltipPrimitive.Root {...props}>{children}</TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  )
}

export const TooltipTrigger = TooltipPrimitive.Trigger
export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, side = 'top', align = 'center', sideOffset = 4, children, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      side={side}
      align={align}
      sideOffset={sideOffset}
      className={cn('z-50 rounded-md border border-border bg-popover px-2.5 py-1 text-[12px] font-medium text-popover-foreground shadow-float', className)}
      {...props}
    >
      {children}
    </TooltipPrimitive.Content>
  </TooltipPrimitive.Portal>
))
TooltipContent.displayName = 'TooltipContent'
