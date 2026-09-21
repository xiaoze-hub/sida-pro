/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{ts,tsx}',
    './packages/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        /* Surface 阶梯(2026-09-20): 只表达空间层级, 不表达涨跌。用 <alpha-value> 声明
           ⇒ 支持 bg-s2/60 这类修饰符, 但新代码不该再手调透明度(门禁 R12 棘轮)。 */
        s0: 'hsl(var(--s0) / <alpha-value>)',
        s1: 'hsl(var(--s1) / <alpha-value>)',
        s2: 'hsl(var(--s2) / <alpha-value>)',
        s3: 'hsl(var(--s3) / <alpha-value>)',
        s4: 'hsl(var(--s4) / <alpha-value>)',
        canvas: 'hsl(var(--chart-canvas) / <alpha-value>)',
        role: {
          watch: 'hsl(var(--role-watch) / <alpha-value>)',
          opp: 'hsl(var(--role-opp) / <alpha-value>)',
          risk: 'hsl(var(--role-risk) / <alpha-value>)',
          system: 'hsl(var(--role-system) / <alpha-value>)',
        },
        heat: {
          n5: 'var(--heat-n5)', n4: 'var(--heat-n4)', n3: 'var(--heat-n3)',
          n2: 'var(--heat-n2)', n1: 'var(--heat-n1)', zero: 'var(--heat-0)',
          p1: 'var(--heat-p1)', p2: 'var(--heat-p2)', p3: 'var(--heat-p3)',
          p4: 'var(--heat-p4)', p5: 'var(--heat-p5)',
        },
        stock: {
          up: '#E53935',
          down: '#43A047',
        },
        /* GS 买卖语义色(2026-09-12 借鉴 TSP 的涨跌色治理):
           stock-up/down 只用于**价格与K线**; "买入/加仓""卖出/减仓"这类交易动作
           走 gs-go/gs-stop —— 同值不同名, 让"价格涨跌"与"买卖动作"两种语义可分别演进。 */
        gs: {
          go: 'var(--gs-go)',
          stop: 'var(--gs-stop)',
        },
      },
      transitionDuration: {
        instant: 'var(--dur-instant)',
        fast: 'var(--dur-fast)',
        normal: 'var(--dur-normal)',
        slow: 'var(--dur-slow)',
        flash: 'var(--dur-flash)',
      },
      transitionTimingFunction: {
        'sida-out': 'var(--ease-out)',
        'sida-in-out': 'var(--ease-in-out)',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
        /* 大圆角同样从 --radius 派生:.card/.card-hover/.card-subtle 等用 rounded-xl,
           此前吃 Tailwind 默认 12px/16px 不随 --radius 收紧,现统一收敛 */
        xl: 'calc(var(--radius) + 2px)',
        '2xl': 'calc(var(--radius) + 4px)',
      },
    },
  },
  plugins: [require('tailwindcss-animate'), require('@tailwindcss/typography')],
}
