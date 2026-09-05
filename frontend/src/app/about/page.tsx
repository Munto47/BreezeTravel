import Link from 'next/link'
import '../experience.css'

export default function AboutPage() {
  return (
    <main className="experience">
      <header className="e-header">
        <Link href="/" className="e-brand">
          行程查<span>TRIPCHECK</span>
        </Link>
        <Link href="/" className="e-button e-button-quiet">
          返回首页
        </Link>
      </header>
      <article className="e-about">
        <div className="e-eyebrow">关于行程查</div>
        <h1>行程，先看清再出发。</h1>
        <p>
          贴入已有攻略，整理每天的地点与路线，再确认时间是否合适。北京、上海、杭州提供地点与路线核对；其他国内城市提供基础整理。缺少依据的内容会保留待确认。
        </p>
        <section id="privacy">
          <h2>隐私与体验数据</h2>
          <h3>示例和自己的攻略</h3>
          <p>
            北京示例的行程与路线为固定回放，不代表实时查询。显示底图时仍会联网加载高德地图，主动搜索地点时会查询地图服务。
          </p>
          <p>
            使用自己的攻略时，文字会交由外部智能服务整理，地点与路线会通过地图服务查询。当前只接收主动粘贴的文字，不接收截图。
          </p>
          <h3>保存多久</h3>
          <p>
            每份匿名行程从创建起保留 24
            小时。账号内创建或领取的行程，从创建或领取起保留 30
            天；刷新或编辑不会延长保留期限。匿名行程由当前浏览器的安全会话保护，只有访问地址不能获得访问权限。
          </p>
          <h3>你可以删除什么</h3>
          <p>
            删除导入文字，会删除原始攻略及对应的文字记录，保留已经整理的行程和地点。删除整份行程，会清理这份行程及关联数据。
          </p>
          <p>
            原始攻略不会自动进入长期记忆，也不会自动用于训练或评测。相关使用需要单独同意。
          </p>
        </section>
        <section>
          <h2>开发主体</h2>
          <p>新余高新区微风软件工作室</p>
          <p className="e-small e-muted">赣ICP备2026008973号-2</p>
        </section>
        <footer className="e-small e-muted">© 2026 BreezeTravel</footer>
      </article>
    </main>
  )
}
