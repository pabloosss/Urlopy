import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm'

// PODMIEŃ na swoje wartości
const URL = 'https://niuwzfjuwszdzkrayphs.supabase.co'
const ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5pdXd6Zmp1d3N6ZHprcmF5cGhzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjE3MjMwNTYsImV4cCI6MjA3NzI5OTA1Nn0.v5DNFYFEGRgiXhkFzqHYs5hUipoPHv5QSAznzKyKmy8'

export const supabase = createClient(URL, ANON, {
  auth: { persistSession: true, autoRefreshToken: true }
})
