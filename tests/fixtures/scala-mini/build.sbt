ThisBuild / scalaVersion := "2.13.18"

lazy val root = (project in file("."))
  .settings(
    name := "scala-mini",
    libraryDependencies += "org.scalameta" %% "munit" % "1.3.6" % Test
  )
