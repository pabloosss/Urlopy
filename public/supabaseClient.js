import { createClient } from 'https://esm.sh/@supabase/supabase-js'

export const supabase = createClient(
  'https://niuwzfjuwszdzkrayphs.supabase.co',  // ← Twój Project URL
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5pdXd6Zmp1d3N6ZHprcmF5cGhzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE3MjMwNTYsImV4cCI6MjA3NzI5OTA1Nn0.v5DNFYFEGRgiXhkFzqHYs5hUipoPHv5QSAznzKyKmy8' // ← Twój anon public key
)
