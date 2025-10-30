import { supabase } from './supabaseClient.js'

export async function meDays(){
  const uid = (await supabase.auth.getUser()).data.user.id
  const { data } = await supabase.from('users').select('vacation_days,used_days').eq('id',uid).single()
  const total = data?.vacation_days||0, used = data?.used_days||0
  return { left: total - used, total }
}

export async function listLeaves(status=null){
  let q = supabase.from('leaves').select('*').order('created_at',{ascending:false})
  if (status) q = q.eq('status', status)
  return await q
}

export async function submitLeave(p){
  const uid = (await supabase.auth.getUser()).data.user.id
  return await supabase.from('leaves').insert([{ user_id: uid, type: p.type, from: p.from, to: p.to, comment: p.comment }])
}

export async function managerInboxCount(){
  const uid = (await supabase.auth.getUser()).data.user.id
  const { data, error } = await supabase
    .from('leaves')
    .select('id, user_id, status, users!inner(manager_id)')
    .eq('status','submitted')
    .eq('users.manager_id', uid)
  if (error) return { count: 0 }
  return { count: data.length }
}

export async function adminInboxCount(){
  const { data, error } = await supabase.from('leaves').select('id').eq('status','manager_approved')
  if (error) return { count: 0 }
  return { count: data.length }
}

export async function managerApprove(id){
  const uid = (await supabase.auth.getUser()).data.user.id
  return await supabase.from('leaves').update({
    status:'manager_approved',
    manager_approval_at:new Date().toISOString(),
    manager_id:uid
  }).eq('id', id)
}

export async function adminFinalize(id, approved){
  const uid = (await supabase.auth.getUser()).data.user.id
  return await supabase.from('leaves').update({
    status: approved?'approved':'rejected',
    admin_approval_at:new Date().toISOString(),
    admin_id:uid
  }).eq('id', id)
}
