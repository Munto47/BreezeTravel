"""
Memory 系统（Sprint 2）

三层记忆架构：
─────────────────────────────────────────────────────────
1. Session History（会话历史）
   - 实现：LangGraph PostgreSQL Checkpointing
   - 存储：langgraph_checkpoints 表
   - 特点：自动管理，无需手动干预

2. Working Memory（工作记忆）
   - 实现：AgentState.working_context（TypedDict）
   - 存储：RAM，随会话结束消失
   - 用途：当前会话内的结构化偏好追踪（预算、风格、人数）
   - 提取：由 tool_executor 节点在每次工具返回后更新

3. Long-term Memory（长期记忆）
   - 实现：PostgreSQL user_preferences 表 + pgvector 语义检索
   - 存储：跨会话持久化
   - 用途：记录用户的旅行偏好，个性化推荐
   - 写入：Synthesizer 完成后后台异步提取并存储
   - 读取：每次对话开始时加载，注入 ReAct Agent system prompt
─────────────────────────────────────────────────────────
"""
