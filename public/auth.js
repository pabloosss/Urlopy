import { supabase } from './supabaseClient.js'

export async function signIn(email, password){
  if (!email || !password) throw new Error('Podaj email i hasło')
  const { data, error } = await supabase.auth.signInWithPassword({ email, password })
  if (error) throw error
  return data.user
}

export async function signOut(){
  await supabase.auth.signOut()
}

export async function getSessionUser(){
  const { data, error } = await supabase.auth.getUser()
  if (error) return null
  return data.user || null
}

